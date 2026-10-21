"""Load saved outputs into Postgres. No connection is made when imported."""

import argparse
from contextlib import closing
import csv
import json
import os
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
TABLES = ('runs', 'records', 'issues', 'membership', 'ranking', 'claims', 'memo', 'verify_summary')
RECORD_COLUMNS = ('review_id', 'run_id', 'source_sha256', 'status', 'reason', 'topic', 'intent',
                  'severity', 'sentiment', 'needs_review', 'evidence_quote', 'cache_source_id',
                  'label_config', 'review_rating', 'review_timestamp', 'app_version')


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def csv_rows(path):
    with path.open(encoding='utf-8', newline='') as stream:
        yield from csv.DictReader(stream)


def record_rows(db, run_id, label_config):
    rows = db.execute('''SELECT r.*,e.result_json,e.cache_source_id FROM records r
        LEFT JOIN enrichment_results e ON e.review_id=r.review_id AND e.label_config=?
        ORDER BY r.source_row''', (label_config,))
    for row in rows:
        labels = json.loads(row['result_json']) if row['status']=='completed' and row['result_json'] else {}
        if row['status']=='completed' and not labels:
            raise ValueError('Completed record has no result under the saved label_config')
        values = dict(row)
        values.update(labels)
        values.update(run_id=run_id, label_config=label_config if labels else None,
                      cache_source_id=row['cache_source_id'] if labels else None)
        yield tuple(values.get(k) for k in RECORD_COLUMNS)


def verifier_summary(db, folder, config):
    predictions = folder/'verify/verifier_predictions.jsonl'
    sample = folder/'verify/sample.json'
    if not predictions.exists() or not sample.exists():
        return dict(available=False)
    declaration = read_json(sample)
    if declaration.get('enrichment_label_config') != config:
        return dict(available=False, reason='Verifier sample uses a different enrichment configuration')
    allowed = set(declaration['review_ids'])
    fields = ('topic', 'intent', 'severity', 'sentiment', 'needs_review')
    matches, count, severity_error = dict.fromkeys(fields, 0), 0, 0
    seen = set()
    with predictions.open(encoding='utf-8') as stream:
        for line in stream:
            if not line.strip():
                continue
            prediction = json.loads(line)
            if prediction.get('label_config') != declaration.get('verifier_label_config'):
                continue
            rid = prediction['review_id']
            if rid not in allowed or rid in seen:
                continue
            result = db.execute('''SELECT e.result_json,r.source_sha256 FROM enrichment_results e
                JOIN records r ON r.review_id=e.review_id WHERE e.review_id=? AND e.label_config=?
                AND r.status='completed' ''', (rid, config)).fetchone()
            if not result or prediction.get('source_sha256') != result['source_sha256']:
                continue
            seen.add(rid)
            labels = json.loads(result['result_json'])
            count += 1
            for field in fields:
                matches[field] += labels[field] == prediction[field]
            severity_error += abs(labels['severity']-prediction['severity'])
    return dict(available=True, compared=count, selected=len(allowed), missing=len(allowed)-count,
                agreement={k:v/count if count else None for k,v in matches.items()},
                severity_mean_absolute_error=severity_error/count if count else None,
                sample=declaration)


def copy_rows(cursor, table, columns, rows):
    from psycopg import sql
    statement = sql.SQL('COPY {} ({}) FROM STDIN').format(
        sql.Identifier(table), sql.SQL(',').join(map(sql.Identifier, columns)))
    with cursor.copy(statement) as copy:
        for row in rows:
            copy.write_row(row)


def load(connection, folder, run_id):
    from psycopg import sql
    from psycopg.types.json import Jsonb
    summary = read_json(folder/'run_summary.json')
    if summary.get('run_id') != run_id:
        raise ValueError('Run summary ID mismatch')
    issues = read_json(folder/'issues.json')
    check = read_json(folder/'memo_check.json')
    markdown = (folder/'memo.md').read_text(encoding='utf-8')
    scope = folder/'scope_ids.txt'
    scope_count = len(scope.read_text(encoding='utf-8').splitlines()) if scope.exists() else None
    path = folder/'pipeline.db'
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')  # One consistent read snapshot; never modifies the saved run.
        with connection.transaction(), connection.cursor() as cur:
            cur.execute('DELETE FROM runs WHERE run_id=%s', (run_id,))
            cur.execute('INSERT INTO runs(run_id,summary,declared_scope_count) VALUES (%s,%s,%s)',
                        (run_id, Jsonb(summary), scope_count))
            copy_rows(cur, 'records', RECORD_COLUMNS, record_rows(db, run_id, summary['label_config']))
            copy_rows(cur, 'issues', ('run_id','issue_id','name','description','representative_ids','member_count'),
                      ((run_id,r['issue_id'],r['name'],r['description'],Jsonb(r['representative_ids']),r['member_count']) for r in issues))
            for table, columns in (
                    ('membership', ('issue_id','review_id')),
                    ('ranking', ('rank','issue_id','complaint_count','severity_sum','mean_severity','priority_score')),
                    ('claims', ('claim_id','issue_id','metric','value'))):
                copy_rows(cur, table, ('run_id',)+columns,
                          ((run_id,) + tuple(row[k] for k in columns) for row in csv_rows(folder/(table+'.csv'))))
            cur.execute('INSERT INTO memo VALUES (%s,%s,%s)', (run_id,markdown,Jsonb(check)))
            cur.execute('INSERT INTO verify_summary VALUES (%s,%s)',
                        (run_id,Jsonb(verifier_summary(db,folder,summary['label_config']))))
            counts = {}
            for table in TABLES:
                cur.execute(sql.SQL('SELECT COUNT(*) FROM {} WHERE run_id=%s').format(sql.Identifier(table)), (run_id,))
                counts[table] = cur.fetchone()[0]
    for table, count in counts.items():
        print(f'{table}: {count}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id')
    parser.add_argument('--schema-only', action='store_true')
    args = parser.parse_args()
    if not args.schema_only and (not args.run_id or args.run_id in ('.','..') or any(c in args.run_id for c in '/\\:')):
        parser.error('Supply a valid --run-id or --schema-only')
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env')
    url = os.environ.get('DATABASE_URL')
    if not url:
        parser.exit(1, 'DATABASE_URL is not configured.\n')
    try:
        import psycopg
        with psycopg.connect(url, connect_timeout=15) as connection:
            if args.schema_only:
                connection.execute((ROOT/'db/schema.sql').read_text(encoding='utf-8'))
                print('Schema created.')
            else:
                load(connection, ROOT/'runs'/args.run_id, args.run_id)
    except Exception as exc:
        # Provider connection errors can include credentials; do not print them.
        parser.exit(1, f'Load failed ({type(exc).__name__}); transaction rolled back. Database URL withheld.\n')


if __name__ == '__main__':
    main()
