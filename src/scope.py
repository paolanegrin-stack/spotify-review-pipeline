"""Declare a deterministic scope and explicitly quarantine excluded pending IDs."""

import argparse
import csv
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from .prepare import ROOT
from .file_io import atomic_text


def clause(db, alias='r'):
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='scope_ids'").fetchone():
        return f'{alias}.review_id IN (SELECT review_id FROM scope_ids)'
    return '1=1'


def install(path, scope_file):
    ids = Path(scope_file).read_text(encoding='utf-8').splitlines()
    if not ids or len(ids) != len(set(ids)) or any(not rid for rid in ids):
        raise ValueError('Scope IDs must be nonempty and unique')
    with closing(sqlite3.connect(path)) as db, db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='scope_ids'").fetchone():
            saved = {r[0] for r in db.execute('SELECT review_id FROM scope_ids')}
            if saved != set(ids):
                raise ValueError('Cannot change an existing declared scope')
            return
        db.execute('CREATE TABLE scope_ids (review_id TEXT PRIMARY KEY)')
        db.executemany('INSERT INTO scope_ids VALUES (?)', ((rid,) for rid in ids))
        count = db.execute('SELECT COUNT(*) FROM scope_ids s JOIN records r ON r.review_id=s.review_id '
                           "WHERE trim(r.review_text) != ''").fetchone()[0]
        if count != len(ids):
            raise ValueError('Scope contains missing or empty-text records')


def select_ids(rows, seed, count):
    ranked = [(hashlib.sha256((seed + ':' + r['review_id']).encode('utf-8')).hexdigest(), r['review_id'])
              for r in rows if r['review_text'].strip()]
    if len(ranked) < count:
        raise ValueError('Not enough nonempty reviews')
    ranked.sort()
    return [rid for _, rid in ranked[:count]]


def snapshot(path, name):
    with closing(sqlite3.connect(path)) as db:
        condition = clause(db)
        ids = sorted(r[0] for r in db.execute(
            f"SELECT r.review_id FROM records r WHERE r.status='completed' AND {condition}"))
        pending = db.execute(f"SELECT COUNT(*) FROM records r WHERE r.status='pending' AND {condition}").fetchone()[0]
    atomic_text(path.parent / name, json.dumps(dict(completed_ids=ids, count=len(ids),
                timestamp=datetime.now(timezone.utc).isoformat()), indent=2) + '\n')
    return len(ids), pending


def quarantine(path):
    with closing(sqlite3.connect(path)) as db, db:
        if clause(db) == '1=1':
            raise ValueError('No declared scope')
        if db.execute("SELECT COUNT(*) FROM records r WHERE r.status='pending' AND " + clause(db)).fetchone()[0]:
            raise ValueError('Finish the scoped run before quarantining excluded IDs')
        count = db.execute("UPDATE records SET status='quarantined',reason='not_classified_out_of_declared_scope' "
                           "WHERE status='pending' AND review_id NOT IN (SELECT review_id FROM scope_ids)").rowcount
    print(f'Quarantined {count} pending records outside the declared scope; no labels or API calls.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--input', type=Path, default=ROOT/'data/spotify_reviews_18months.csv')
    parser.add_argument('--quarantine-out-of-scope', action='store_true')
    args = parser.parse_args()
    if not args.run_id or args.run_id in ('.', '..') or any(c in args.run_id for c in '/\\:'):
        raise ValueError('Invalid run ID')
    path = ROOT/'runs'/args.run_id/'pipeline.db'
    if not path.is_file():
        raise ValueError('Run prepare first')
    if args.quarantine_out_of_scope:
        quarantine(path)
        return
    manifest = json.loads((ROOT/'data/manifest.json').read_text(encoding='utf-8'))
    seed = manifest['samples']['seed']
    csv.field_size_limit(2147483647)
    with args.input.open(encoding='utf-8', newline='') as stream:
        ids = select_ids(csv.DictReader(stream), seed, 100000)
    with (ROOT/'data/golden_50_to_label.csv').open(encoding='utf-8', newline='') as stream:
        golden = {r['review_id'] for r in csv.DictReader(stream)}
    if len(golden) != 50 or not golden.issubset(ids):
        raise ValueError('FAIL: golden 50 IDs are not all included')
    target = path.parent/'scope_ids.txt'
    atomic_text(target, '\n'.join(ids) + '\n')
    install(path, target)
    atomic_text(ROOT/'docs/scope.md', '# Declared final scope\n\n'
                f'Select the first 100,000 nonempty-text reviews by ascending SHA-256 of UTF-8 '
                f'`{seed}:review_id`, preserving exact review IDs. Ties use review ID. '
                'All 50 golden IDs are included; human labels are never read or sent to a model.\n\n'
                '100,000 is the assignment minimum declared by the user for this run. '
                'The checked-in GRADING_CONTRACT.md instead says classify all 660,609 nonempty rows; '
                'this declared scope is a change from that file. Excluded records receive no labels.\n\n'
                'Only scoped IDs may be enriched or receive duplicate-text cache results. '
                'Downstream stages use completed scoped records. After completion, explicitly run '
                '`python -m src.scope --run-id full_v1 --quarantine-out-of-scope` to account for excluded pending IDs.\n')
    print(f'Selected {len(ids):,} nonempty review IDs; golden IDs included: {len(golden)}/50 PASS. Saved {target}')


if __name__ == '__main__':
    main()
