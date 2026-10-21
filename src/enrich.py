"""Saved-state enrichment. Import and --dry-run never create an API client."""

import argparse
from contextlib import contextmanager, closing
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import random
import sqlite3
import sys
import threading
import time
import uuid

from . import config as C
from .prepare import ROOT, canonical


class BudgetStop(Exception):
    pass


class FatalAPIError(Exception):
    """Permanent request/schema rejection: stop dispatch without retrying."""


def object_schema(properties):
    return dict(type='object', properties=properties, required=list(properties), additionalProperties=False)


def build_request(batch, validation_error=None):
    if not 1 <= len(batch) <= min(C.BATCH_SIZE, C.MAX_BATCH_SIZE, 50):
        raise ValueError('Batch size exceeds configured or hard maximum')
    local = {f'r{i}': row for i, row in enumerate(batch, 1)}
    variants = []
    for long in (False, True):
        ids = [key for key, row in local.items() if (len(row['review_text']) > C.SHORT_TEXT_CHARS) == long]
        if not ids:
            continue
        properties = dict(
            id=dict(type='string', enum=ids), topic=dict(type='string', enum=list(C.TOPICS)),
            intent=dict(type='string', enum=list(C.INTENTS)),
            severity=dict(type='integer', minimum=1, maximum=5),
            sentiment=dict(type='number', enum=list(C.SENTIMENTS)),
            entities=dict(type='array', maxItems=C.MAX_ENTITIES,
                          items=dict(type='string', minLength=1, maxLength=C.MAX_ENTITY_CHARS)),
            needs_review=dict(type='boolean'))
        if long:
            properties['evidence_quote'] = dict(type='string', minLength=1, maxLength=C.MAX_QUOTE_CHARS)
        variants.append(object_schema(properties))
    schema = object_schema(dict(records=dict(type='array', minItems=len(batch), maxItems=len(batch),
                                            items=variants[0] if len(variants) == 1 else dict(anyOf=variants))))
    message = dict(reviews=[dict(id=key, review_text=row['review_text']) for key, row in local.items()])
    if validation_error:
        message['validation_error'] = validation_error
    request = dict(model=C.MODEL, reasoning=dict(effort=C.EFFORT),
                   instructions=(ROOT / 'prompts' / f'{C.PROMPT_VERSION}.md').read_text(encoding='utf-8'),
                   input=canonical(message), max_output_tokens=C.MAX_OUTPUT_TOKENS,
                   text=dict(format=dict(type='json_schema', name='enrich_v1', strict=True, schema=schema)),
                   store=False)
    return request, local


def validate_response(payload, local):
    """Return valid local-ID results and per-ID errors; never silently lose an ID."""
    errors, valid = {}, {}
    if not isinstance(payload, dict) or set(payload) != {'records'} or not isinstance(payload['records'], list):
        return {}, {key: 'Expected object with exactly one records array' for key in local}
    grouped = {key: [] for key in local}
    for item in payload['records']:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str) or item['id'] not in local:
            return {}, {key: 'Foreign or malformed response ID' for key in local}
        grouped[item['id']].append(item)
    for key, items in grouped.items():
        try:
            if len(items) != 1:
                raise ValueError('Missing or duplicate ID: ' + key)
            item, text = items[0], local[key]['review_text']
            required = {'id', 'topic', 'intent', 'severity', 'sentiment', 'entities', 'needs_review'}
            long = len(text) > C.SHORT_TEXT_CHARS
            if long:
                required.add('evidence_quote')
            if set(item) != required:
                raise ValueError('Missing or unexpected keys')
            if item['topic'] not in C.TOPICS or item['intent'] not in C.INTENTS:
                raise ValueError('Invalid topic or intent')
            if type(item['severity']) is not int or not 1 <= item['severity'] <= 5:
                raise ValueError('Severity must be integer 1-5')
            if type(item['sentiment']) not in (int, float) or item['sentiment'] not in C.SENTIMENTS:
                raise ValueError('Invalid sentiment')
            if type(item['needs_review']) is not bool:
                raise ValueError('needs_review must be boolean')
            entities = item['entities']
            if (not isinstance(entities, list) or len(entities) > C.MAX_ENTITIES
                    or any(not isinstance(x, str) or not x.strip() or len(x) > C.MAX_ENTITY_CHARS for x in entities)):
                raise ValueError('Invalid entities')
            quote = item['evidence_quote'] if long else text
            if not isinstance(quote, str) or not quote.strip() or quote not in text or (long and len(quote) > C.MAX_QUOTE_CHARS):
                raise ValueError('Quote must be a short exact source substring')
            valid[key] = dict({k: v for k, v in item.items() if k != 'id'}, evidence_quote=quote)
        except (ValueError, TypeError) as exc:
            errors[key] = str(exc)
    return valid, errors


def input_estimate(request):
    # Conservative byte-based upper estimate, not measured tokenizer usage.
    return len(canonical(request).encode('utf-8')) + C.INPUT_OVERHEAD_TOKENS


def read_rates():
    with (ROOT / 'cost' / 'rates.csv').open(encoding='utf-8', newline='') as stream:
        rates = {r['item']: Decimal(r['price_usd_per_million_tokens']) if r['price_usd_per_million_tokens'] else None
                 for r in csv.DictReader(stream) if r['provider'] == 'openai' and r['model'] == C.MODEL}
    for key in ('input', 'output'):
        if rates.get(key) is None or not rates[key].is_finite() or rates[key] < 0:
            raise ValueError('Missing or invalid rate: ' + key)
    if rates.get('cached_input') is not None and (not rates['cached_input'].is_finite() or rates['cached_input'] < 0):
        raise ValueError('Invalid cached-input rate')
    return rates


@contextmanager
def connect(path):
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()


def initialize(db):
    db.executescript('''
        CREATE INDEX IF NOT EXISTS records_text_hash ON records(text_sha256);
        CREATE TABLE IF NOT EXISTS enrichment_results (
            review_id TEXT NOT NULL, label_config TEXT NOT NULL, text_sha256 TEXT NOT NULL,
            result_json TEXT NOT NULL, cache_source_id TEXT,
            PRIMARY KEY(review_id,label_config));
        CREATE INDEX IF NOT EXISTS results_cache ON enrichment_results(text_sha256,label_config);
        CREATE TABLE IF NOT EXISTS enrichment_calls (
            call_id TEXT PRIMARY KEY, liability_usd TEXT NOT NULL, event_json TEXT NOT NULL);
    ''')


class Ledger:
    def __init__(self, path, cap, rates, role='enrich', label_config=None):
        self.path, self.cap, self.rates = path, cap, rates
        self.role = role
        self.label_config = label_config or C.LABEL_CONFIG
        self.lock = threading.Lock()
        self.stopped = threading.Event()

    def export(self, db):
        target = self.path.parent / 'calls.jsonl'
        temporary = target.with_suffix('.jsonl.tmp')
        with temporary.open('w', encoding='utf-8', newline='\n') as stream:
            for row in db.execute('SELECT event_json FROM enrichment_calls ORDER BY rowid'):
                stream.write(row[0] + '\n')
        temporary.replace(target)

    def reserve(self, batch, request, local, phase):
        tokens = input_estimate(request)
        if tokens > C.MAX_INPUT_TOKENS:
            raise BudgetStop('Input token upper estimate exceeds configured limit')
        cost = (tokens * max(self.rates['input'], self.rates.get('cached_input') or self.rates['input'])
                + C.MAX_OUTPUT_TOKENS * self.rates['output']) / Decimal(1000000)
        call_id = 'local-' + uuid.uuid4().hex
        event = dict(request_id=call_id, role=self.role, review_ids=[r['review_id'] for r in batch],
                     model=C.MODEL, effort=C.EFFORT, label_config=self.label_config, phase=phase,
                     outcome='failed', error='in_flight_or_interrupted_usage_unknown',
                     input_tokens=None, cached_input_tokens=None, output_tokens=None,
                     latency_seconds=None, timestamp=datetime.now(timezone.utc).isoformat(),
                     reserved_usd=str(cost), actual_cost_usd=None, request=request,
                     pricing_basis='user-supplied rates dated 2026-10-20; to confirm against the official page',
                     local_id_map={key: row['review_id'] for key, row in local.items()})
        with self.lock, connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            spent = sum((Decimal(r[0]) for r in db.execute('SELECT liability_usd FROM enrichment_calls')), Decimal(0))
            if self.stopped.is_set() or spent + cost > self.cap:
                self.stopped.set()
                raise BudgetStop(f'Budget stop: spent/reserved {spent} + next {cost} > cap {self.cap}')
            db.execute('INSERT INTO enrichment_calls VALUES (?,?,?)', (call_id, str(cost), canonical(event)))
            for row in batch if self.role == 'enrich' else []:
                db.execute('UPDATE records SET attempts=attempts+1 WHERE text_sha256=? AND review_text=?',
                           (row['text_sha256'], row['review_text']))
            db.commit()
            self.export(db)
        return call_id, event, cost

    def finish(self, call_id, event, reserved, response, error, latency):
        event.update(latency_seconds=latency, outcome='failed' if error else 'succeeded', error=error)
        liability = reserved
        if response is not None:
            event['provider_request_id'] = getattr(response, '_request_id', None) or response.id
            event['response'] = response.model_dump(mode='json')
            usage = response.usage
            if usage is not None:
                cached = getattr(usage.input_tokens_details, 'cached_tokens', None)
                event.update(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                             cached_input_tokens=cached)
                reasoning = getattr(usage.output_tokens_details, 'reasoning_tokens', None)
                if reasoning is not None:
                    event['reasoning_tokens'] = reasoning
                if cached is not None and (not cached or self.rates.get('cached_input') is not None):
                    liability = ((usage.input_tokens - cached) * self.rates['input']
                                 + cached * (self.rates.get('cached_input') or Decimal(0))
                                 + usage.output_tokens * self.rates['output']) / Decimal(1000000)
                    event['actual_cost_usd'] = str(liability)
        with self.lock, connect(self.path) as db:
            db.execute('UPDATE enrichment_calls SET liability_usd=?,event_json=? WHERE call_id=?',
                       (str(liability), canonical(event), call_id))
            db.commit()
            self.export(db)
        if liability > reserved:
            self.stopped.set()
            raise BudgetStop('Actual usage exceeded conservative reservation; stopped for reconciliation')


def call_batch(batch, ledger, phase, client, validation_error=None):
    from openai import APIConnectionError, APIStatusError, RateLimitError
    request, local = build_request(batch, validation_error)
    for attempt in range(C.MAX_API_ATTEMPTS):
        call_id, event, reserved = ledger.reserve(batch, request, local, phase)
        started = time.monotonic()
        response, error, retry, fatal = None, None, False, False
        try:
            response = client.responses.create(**request)
        except (APIConnectionError, APIStatusError) as exc:
            error = str(exc)
            event['error_type'] = type(exc).__name__
            event['error_body'] = getattr(exc, 'body', None)
            event['provider_request_id'] = getattr(exc, 'request_id', None)
            code = getattr(exc, 'status_code', None)
            event['http_status'] = code
            # Stop on all permanent request-validation failures, including schema
            # rejections even when the server's wording does not name a keyword.
            fatal = code in (400, 422) or ('schema' in error.lower() and
                      any(word in error.lower() for word in ('invalid', 'unsupported', 'not permitted', 'not supported')))
            retry = isinstance(exc, (APIConnectionError, RateLimitError)) or code in (408, 409) or (code is not None and code >= 500)
        except Exception as exc:
            error = type(exc).__name__
        if error:
            if fatal:
                ledger.stopped.set()
            ledger.finish(call_id, event, reserved, response, error, time.monotonic() - started)
            if fatal:
                raise FatalAPIError(error)
            if retry and attempt + 1 < C.MAX_API_ATTEMPTS:
                time.sleep(min(C.MAX_BACKOFF_SECONDS, C.BACKOFF_SECONDS * 2 ** attempt) + random.random())
                continue
            return {}, {key: 'api_error:' + error for key in local}, local
        try:
            if response.status != 'completed':
                raise ValueError('Response incomplete or refused')
            payload = json.loads(response.output_text)
        except (ValueError, TypeError):
            ledger.finish(call_id, event, reserved, response, 'invalid_json_refusal_or_incomplete', time.monotonic() - started)
            return {}, {key: 'Invalid JSON, refusal, or incomplete output' for key in local}, local
        valid, errors = validate_response(payload, local)
        event['validation_errors'] = errors
        event['validated_review_ids'] = [local[key]['review_id'] for key in valid]
        ledger.finish(call_id, event, reserved, response,
                      'validation_failed' if errors and not valid else None, time.monotonic() - started)
        return valid, errors, local
    raise RuntimeError('Unreachable attempt loop')


def save_batch(path, outcomes):
    """All results/status changes for this batch commit together, including aliases."""
    with connect(path) as db:
        for original, result, reason in outcomes:
            matches = db.execute('SELECT * FROM records WHERE text_sha256=? AND review_text=?',
                                 (original['text_sha256'], original['review_text'])).fetchall()
            for row in matches:
                if result is not None:
                    alias = None if row['review_id'] == original['review_id'] else original['review_id']
                    db.execute('INSERT OR REPLACE INTO enrichment_results VALUES (?,?,?,?,?)',
                               (row['review_id'], C.LABEL_CONFIG, row['text_sha256'], canonical(result), alias))
                    db.execute("UPDATE records SET status='completed',reason=NULL WHERE source_row=?", (row['source_row'],))
                else:
                    db.execute("UPDATE records SET status='quarantined',reason=? WHERE source_row=?", (reason, row['source_row']))


def process_batch(batch, ledger, phase, client):
    outcomes = []
    try:
        valid, errors, local = call_batch(batch, ledger, phase, client)
        for key, result in valid.items():
            outcomes.append((local[key], result, None))
        for key, error in errors.items():
            row = local[key]
            if error.startswith('api_error:'):
                outcomes.append((row, None, error))
                continue
            try:
                # Singleton retry is smaller than the normal 25-item request.
                fixed, remaining, _ = call_batch([row], ledger, phase, client, error)
                if fixed:
                    outcomes.append((row, fixed['r1'], None))
                else:
                    outcomes.append((row, None, 'validation_failed:' + remaining['r1']))
            except (BudgetStop, FatalAPIError):
                save_batch(ledger.path, outcomes)
                raise
    except (BudgetStop, FatalAPIError):
        ledger.stopped.set()
        raise
    save_batch(ledger.path, outcomes)


def pending(db, limit):
    # Configuration-specific saved results are reusable only for exact text.
    query = '''SELECT r.* FROM records r
        WHERE r.status != 'quarantined' AND NOT EXISTS (
            SELECT 1 FROM enrichment_results e WHERE e.review_id=r.review_id AND e.label_config=?)
        AND r.source_row=(SELECT MIN(x.source_row) FROM records x
                         WHERE x.text_sha256=r.text_sha256 AND x.review_text=r.review_text)
        ORDER BY r.source_row'''
    rows = [dict(row) for row in db.execute(query, (C.LABEL_CONFIG,))]
    return rows if limit is None else rows[:limit]


def reuse_saved(path):
    """Reconcile pending aliases using direct originals, including after restart."""
    with connect(path) as db:
        rows = db.execute('''SELECT DISTINCT e.result_json, r.* FROM enrichment_results e
            JOIN records r ON r.review_id=e.review_id
            WHERE e.label_config=? AND e.cache_source_id IS NULL AND EXISTS (
                SELECT 1 FROM records p WHERE p.text_sha256=r.text_sha256
                AND p.review_text=r.review_text AND p.status='pending')''', (C.LABEL_CONFIG,)).fetchall()
    for row in rows:
        save_batch(path, [(dict(row), json.loads(row['result_json']), None)])


def snapshot(path):
    """Read-only baseline for per-invocation accounting; never creates a database."""
    state = dict(statuses=None, last_call=0, configured_results=0, events=[])
    if path is None or not path.is_file():
        return state
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        state['statuses'] = {status: count for status, count in db.execute(
            'SELECT status,COUNT(*) FROM records GROUP BY status')}
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'enrichment_results' in tables:
            state['configured_results'] = db.execute(
                'SELECT COUNT(*) FROM enrichment_results WHERE label_config=?', (C.LABEL_CONFIG,)).fetchone()[0]
        if 'enrichment_calls' in tables:
            state['last_call'] = db.execute('SELECT COALESCE(MAX(rowid),0) FROM enrichment_calls').fetchone()[0]
    return state


def print_summary(args, path, before, started):
    print(f'Final summary | run id: {args.run_id} | phase: {args.phase} | dry_run: {args.dry_run}')
    try:
        after = snapshot(path)
        events = []
        if path is not None and path.is_file() and after['last_call']:
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                events = [json.loads(row[0]) for row in db.execute(
                    'SELECT event_json FROM enrichment_calls WHERE rowid>? ORDER BY rowid', (before['last_call'],))]
        attempted = len({rid for event in events for rid in event.get('review_ids', [])})
        statuses = after['statuses']
        if statuses is None:
            print('Records: unavailable (run database does not exist).')
        else:
            previous = before['statuses'] or {}
            completed = max(0, after['configured_results'] - before['configured_results'],
                            statuses.get('completed', 0) - previous.get('completed', 0))
            quarantined = max(0, statuses.get('quarantined', 0) - previous.get('quarantined', 0))
            print(f'Records this invocation: attempted={attempted} (unique IDs sent); '
                  f'completed={completed} (including exact-text reuse); quarantined={quarantined}; '
                  f'still pending={statuses.get("pending", 0)} (whole database).')
            print('Database totals: ' + '; '.join(f'{key}={statuses.get(key, 0)}'
                  for key in ('pending', 'completed', 'quarantined')))
        print(f'Calls this invocation: {len(events)} '
              f'(succeeded={sum(e.get("outcome") == "succeeded" for e in events)}, '
              f'failed={sum(e.get("outcome") == "failed" for e in events)}).')
        for field in ('input_tokens', 'cached_input_tokens', 'output_tokens'):
            known = [e[field] for e in events if type(e.get(field)) is int and e[field] >= 0]
            missing = len(events) - len(known)
            suffix = f'; unavailable for {missing} call(s), known subtotal only' if missing else ''
            print(f'Real API {field}: {sum(known)}{suffix}')
        known_cost, unresolved = Decimal(0), 0
        rates = read_rates() if events else None
        for event in events:
            inp, cached, out = (event.get(k) for k in ('input_tokens', 'cached_input_tokens', 'output_tokens'))
            if (event.get('model') != C.MODEL or any(type(v) is not int or v < 0 for v in (inp, cached, out))
                    or cached > inp or (cached and rates.get('cached_input') is None)):
                unresolved += 1
                continue
            known_cost += ((inp - cached) * rates['input'] + cached * (rates.get('cached_input') or Decimal(0))
                           + out * rates['output']) / Decimal(1000000)
        suffix = f'; TOTAL UNKNOWN: usage/rates unavailable for {unresolved} call(s)' if unresolved else ''
        print(f'Estimated cost USD this invocation (cost/rates.csv): ${known_cost:.8f}{suffix}')
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError) as exc:
        print('Summary details unavailable: ' + str(exc))
    finally:
        print(f'Wall-clock seconds this invocation: {time.monotonic() - started:.3f}')


def run(args):
    started = time.monotonic()
    path = None
    before = dict(statuses=None, last_call=0, configured_results=0, events=[])
    try:
        if (args.run_id and Path(args.run_id).name == args.run_id and args.run_id not in ('.', '..')
                and ':' not in args.run_id and '\\' not in args.run_id):
            path = ROOT / 'runs' / args.run_id / 'pipeline.db'
            before = snapshot(path)
        return execute_run(args)
    finally:
        print_summary(args, path, before, started)


def execute_run(args):
    if not args.run_id or Path(args.run_id).name != args.run_id or args.run_id in ('.', '..') or ':' in args.run_id or '\\' in args.run_id:
        raise ValueError('Invalid run ID')
    if not 1 <= C.BATCH_SIZE <= min(C.MAX_BATCH_SIZE, 50) or not 1 <= args.workers <= C.MAX_WORKERS:
        raise ValueError('Invalid batch size or workers')
    if args.limit is not None and args.limit < 1:
        raise ValueError('limit must be positive (counts distinct texts)')
    cap = Decimal(args.max_spend_usd)
    if not cap.is_finite() or cap < 0:
        raise ValueError('Spend cap must be finite and nonnegative')
    path = ROOT / 'runs' / args.run_id / 'pipeline.db'
    if not path.is_file():
        raise ValueError('Run database missing; run prepare first')
    # Read-only dry-run: no migrations, credentials, logs, or state changes.
    if args.dry_run:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            has_results = db.execute("SELECT 1 FROM sqlite_master WHERE name='enrichment_results'").fetchone()
            if has_results:
                batch = pending(db, min(args.limit or C.BATCH_SIZE, C.BATCH_SIZE))
            else:
                batch = [dict(r) for r in db.execute("SELECT * FROM records WHERE status='pending' GROUP BY text_sha256 ORDER BY MIN(source_row) LIMIT ?", (min(args.limit or C.BATCH_SIZE, C.BATCH_SIZE),))]
        if not batch:
            print('No pending texts.')
            return
        request, _ = build_request(batch)
        output = json.dumps(request, ensure_ascii=False, indent=2)
        print(output)
        print('Estimated input tokens — byte-based upper bound (UTF-8 bytes + overhead; not API usage):', input_estimate(request))
        print('Real token counts come only from API response usage fields.')
        return
    if cap == 0:
        print('Budget stop: default/explicit spend cap is zero; no API client or call created.')
        return
    lock_path = path.parent / '.enrich.lock'
    lock = lock_path.open('x')
    lock.close()
    try:
        with connect(path) as db:
            initialize(db)
        reuse_saved(path)
        with connect(path) as db:
            rows = pending(db, args.limit)
        ledger = Ledger(path, cap, read_rates())
        with connect(path) as db:
            ledger.export(db)
        if not rows:
            print('No pending texts under ' + C.LABEL_CONFIG)
            return
        from dotenv import load_dotenv
        from openai import OpenAI
        load_dotenv(ROOT / '.env')
        with OpenAI(max_retries=0, timeout=C.TIMEOUT_SECONDS) as client:
            fatal_error = None
            # At most workers batches in flight; new submissions stop at budget boundary.
            batches = iter(rows[i:i + C.BATCH_SIZE] for i in range(0, len(rows), C.BATCH_SIZE))
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                active = set()
                for _ in range(args.workers):
                    batch = next(batches, None)
                    if batch:
                        active.add(pool.submit(process_batch, batch, ledger, args.phase, client))
                while active:
                    future = next(as_completed(active))
                    active.remove(future)
                    try:
                        future.result()
                    except BudgetStop as exc:
                        ledger.stopped.set()
                        print(str(exc))
                    except FatalAPIError as exc:
                        ledger.stopped.set()
                        fatal_error = str(exc)
                        print('Permanent API/schema error; stopping without retry: ' + fatal_error)
                    if not ledger.stopped.is_set():
                        batch = next(batches, None)
                        if batch:
                            active.add(pool.submit(process_batch, batch, ledger, args.phase, client))
            if fatal_error is not None:
                raise SystemExit(1)
    finally:
        lock_path.unlink(missing_ok=True)


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--max-spend-usd', default=C.DEFAULT_MAX_SPEND_USD)
    parser.add_argument('--workers', type=int, default=C.WORKERS)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--phase', choices=('initial', 'resume'), default='initial')
    run(parser.parse_args())


if __name__ == '__main__':
    main()
