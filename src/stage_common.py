"""Offline helpers and shared, budgeted transport for verify/group. No import-time calls."""

from contextlib import contextmanager, closing
from decimal import Decimal
import csv
import hashlib
import io
import json
import random
import sqlite3
import time

from . import config as C
from .prepare import ROOT, canonical
from .enrich import (BudgetStop, FatalAPIError, Ledger, connect, initialize,
                     input_estimate, read_rates, snapshot, print_summary)


def run_path(run_id):
    if not run_id or run_id in ('.', '..') or any(c in run_id for c in '/\\:'):
        raise ValueError('run-id must be a single directory name')
    path = ROOT / 'runs' / run_id / 'pipeline.db'
    if not path.is_file():
        raise ValueError('Run database missing; run prepare first')
    return path


def completed_records(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='enrichment_results'").fetchone()
        if not exists:
            return []
        rows = db.execute('''SELECT r.*,e.result_json FROM records r JOIN enrichment_results e
            ON r.review_id=e.review_id WHERE r.status='completed' AND e.label_config=?
            ORDER BY r.review_id''', (C.LABEL_CONFIG,))
        output = []
        for row in rows:
            item = dict(row)
            item['labels'] = json.loads(item.pop('result_json'))
            output.append(item)
        if len({r['review_id'] for r in output}) != len(output):
            raise ValueError('Duplicate completed review IDs')
        return output


def atomic_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    with tmp.open('w', encoding='utf-8', newline='\n') as stream:
        stream.write(text)
    tmp.replace(path)


def csv_text(fields, rows):
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator='\n')
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def request(role, user, schema):
    if role not in ('verify', 'group', 'memo'):
        raise ValueError('Unsupported stage role')
    model = getattr(C, role.upper() + '_MODEL')
    effort = getattr(C, role.upper() + '_EFFORT')
    version = getattr(C, role.upper() + '_PROMPT_VERSION')
    return dict(model=model, reasoning=dict(effort=effort), store=False,
                instructions=(ROOT / 'prompts' / f'{version}.md').read_text(encoding='utf-8'),
                input=canonical(user), max_output_tokens=C.MAX_OUTPUT_TOKENS,
                text=dict(format=dict(type='json_schema', name=version, strict=True, schema=schema)))


def preview(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))
    print('Estimated input tokens — byte-based upper bound (UTF-8 bytes + overhead; not API usage):', input_estimate(value))
    print('Real token counts come only from API response usage fields.')


def cache_key(role, value, rows):
    return digest(dict(role=role, request=value, source_rows=[(r['review_id'], r['source_sha256']) for r in rows]))


def cached(path, role, key):
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='stage_cache'").fetchone():
            return None
        row = db.execute('SELECT payload FROM stage_cache WHERE role=? AND cache_key=?', (role, key)).fetchone()
        return json.loads(row[0]) if row else None


def save_cache(path, role, key, result):
    # The authoritative batch result is committed as one SQLite transaction.
    with connect(path) as db:
        db.execute('INSERT OR REPLACE INTO stage_cache VALUES (?,?,?)', (role, key, canonical(result)))


@contextmanager
def live_stage(path, role, cap):
    cap = Decimal(cap)
    if not cap.is_finite() or cap < 0:
        raise ValueError('Spend cap must be finite and nonnegative')
    # Shared with enrichment: do not let separate stage processes race log exports.
    lock_path = path.parent / '.enrich.lock'
    lock = lock_path.open('x')
    lock.close()
    try:
        with connect(path) as db:
            initialize(db)
            db.execute('''CREATE TABLE IF NOT EXISTS stage_cache (
                role TEXT,cache_key TEXT,payload TEXT,PRIMARY KEY(role,cache_key))''')
        config = getattr(C, role.upper() + '_LABEL_CONFIG')
        ledger = Ledger(path, cap, read_rates(), role=role, label_config=config)
        with connect(path) as db:
            ledger.export(db)
        yield ledger
    finally:
        lock_path.unlink(missing_ok=True)


def send(value, rows, ledger, phase, validator, client):
    """SDK retries disabled; reserve/log each explicit transport attempt separately."""
    from openai import APIConnectionError, APIStatusError, RateLimitError
    mapping = ({row['review_id']: row for row in rows} if ledger.role == 'group'
               else {f'r{i}': row for i, row in enumerate(rows, 1)})
    for attempt in range(C.MAX_API_ATTEMPTS):
        call_id, event, reserved = ledger.reserve(rows, value, mapping, phase)
        started = time.monotonic()
        response, error, retry, fatal = None, None, False, False
        try:
            response = client.responses.create(**value)
        except (APIConnectionError, APIStatusError) as exc:
            error = str(exc)
            event.update(error_type=type(exc).__name__, error_body=getattr(exc, 'body', None),
                         provider_request_id=getattr(exc, 'request_id', None), http_status=getattr(exc, 'status_code', None))
            code = getattr(exc, 'status_code', None)
            fatal = code in (400, 422) or ('schema' in error.lower() and any(
                word in error.lower() for word in ('invalid', 'unsupported', 'not permitted', 'not supported')))
            retry = isinstance(exc, (APIConnectionError, RateLimitError)) or code in (408, 409) or (code is not None and code >= 500)
        except Exception as exc:
            error = str(exc) or type(exc).__name__
        if error is not None:
            if fatal:
                ledger.stopped.set()
            ledger.finish(call_id, event, reserved, response, error, time.monotonic() - started)
            if fatal:
                raise FatalAPIError(error)
            if retry and attempt + 1 < C.MAX_API_ATTEMPTS:
                time.sleep(min(C.MAX_BACKOFF_SECONDS, C.BACKOFF_SECONDS * 2 ** attempt) + random.random())
                continue
            raise RuntimeError('API request failed after bounded attempts: ' + error)
        try:
            if response.status != 'completed':
                raise ValueError('Response incomplete or refused')
            result = validator(json.loads(response.output_text))
        except (ValueError, TypeError, KeyError) as exc:
            ledger.finish(call_id, event, reserved, response, 'validation_failed: ' + str(exc), time.monotonic() - started)
            raise ValueError('Stage output rejected; no business result saved: ' + str(exc)) from exc
        ledger.finish(call_id, event, reserved, response, None, time.monotonic() - started)
        return result


@contextmanager
def summary(args, role):
    started = time.monotonic()
    path, before = None, dict(statuses=None, last_call=0, configured_results=0)
    try:
        path = run_path(args.run_id)
        before = snapshot(path)
        yield path
    except BudgetStop as exc:
        args.spend_stopped = True
        print('Spend limit stopped ' + role + ': ' + str(exc))
    finally:
        print('Stage: ' + role)
        unit = {'verify': 'reviews', 'group': 'issues', 'memo': 'memos'}[role]
        print(f'Stage units saved/reused: {getattr(args, "stage_completed", 0)}/'
              f'{getattr(args, "stage_total", "unavailable")} '
              f'({unit}); dry-run previews are not completions.')
        print_summary(args, path, before, started)
