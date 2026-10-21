"""Deterministic, offline ingestion. No third-party dependencies or model calls."""

import argparse
import csv
import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

FIELDS = ('review_id', 'review_text', 'review_rating', 'review_likes',
          'app_version', 'review_timestamp')
ROOT = Path(__file__).resolve().parents[1]


def canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'),
                      sort_keys=True, allow_nan=False)


def row_sha(row):
    return hashlib.sha256(canonical([row[k] for k in FIELDS]).encode('utf-8')).hexdigest()


def file_sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True,
                               allow_nan=False) + '\n', encoding='utf-8')


def prepare(input_path, run_id):
    if not run_id or Path(run_id).name != run_id or run_id in ('.', '..') or ':' in run_id or '\\' in run_id:
        raise ValueError('run-id must be a single directory name')
    manifest = json.loads((ROOT / 'data' / 'manifest.json').read_text(encoding='utf-8'))
    expected_file = manifest['files']['spotify_reviews_18months.csv']
    # Refuse to overwrite saved progress from any previous ingestion/enrichment.
    run = ROOT / 'runs' / run_id
    run.mkdir(parents=True, exist_ok=False)
    csv.field_size_limit(2147483647)
    report = dict(total_rows=0, file_bytes=input_path.stat().st_size,
                  file_sha256=file_sha(input_path), empty_review_text_count=0,
                  missing_app_version_count=0, duplicate_review_id_count=0,
                  rating_distribution={}, rows_per_calendar_month={},
                  min_timestamp=None, max_timestamp=None)
    seen = set()
    parsed = hashlib.sha256()
    db = sqlite3.connect(run / 'pipeline.db')
    try:
        db.executescript('''
            CREATE TABLE records (
                source_row INTEGER PRIMARY KEY,
                review_id TEXT NOT NULL,
                review_text TEXT NOT NULL,
                review_rating TEXT NOT NULL,
                review_likes TEXT NOT NULL,
                app_version TEXT NOT NULL,
                review_timestamp TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                text_sha256 TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','completed','quarantined')),
                attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                reason TEXT
            );
            CREATE INDEX records_review_id ON records(review_id);
            CREATE TABLE distinct_texts (
                text_sha256 TEXT NOT NULL,
                source_row INTEGER NOT NULL REFERENCES records(source_row),
                review_id TEXT NOT NULL,
                PRIMARY KEY (text_sha256, source_row)
            );
        ''')
        with input_path.open(newline='', encoding='utf-8') as stream, (run / 'quarantine.jsonl').open('w', encoding='utf-8', newline='\n') as quarantine:
            reader = csv.DictReader(stream, strict=True)
            if reader.fieldnames != list(FIELDS):
                raise ValueError('CSV headers differ from the six contracted fields')
            with db:
                for row in reader:
                    if None in row or any(value is None for value in row.values()):
                        raise ValueError('Malformed CSV row')
                    report['total_rows'] += 1
                    number = report['total_rows']
                    rid, text = row['review_id'], row['review_text']
                    source_hash = row_sha(row)
                    text_hash = hashlib.sha256(text.encode('utf-8')).hexdigest()
                    parsed.update(bytes.fromhex(source_hash))
                    empty = not text.strip()
                    report['empty_review_text_count'] += empty
                    report['missing_app_version_count'] += not row['app_version'].strip()
                    report['duplicate_review_id_count'] += rid in seen
                    seen.add(rid)
                    timestamp = row['review_timestamp']
                    datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S')
                    report['min_timestamp'] = min(report['min_timestamp'], timestamp) if report['min_timestamp'] else timestamp
                    report['max_timestamp'] = max(report['max_timestamp'], timestamp) if report['max_timestamp'] else timestamp
                    for key, value in (('rating_distribution', row['review_rating']),
                                       ('rows_per_calendar_month', timestamp[:7])):
                        report[key][value] = report[key].get(value, 0) + 1
                    status = 'quarantined' if empty else 'pending'
                    reason = 'empty_review_text' if empty else None
                    db.execute('INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                               (number, *(row[k] for k in FIELDS), source_hash, text_hash, status, 0, reason))
                    if empty:
                        quarantine.write(canonical(dict(row, source_sha256=source_hash,
                                                        status=status, reason=reason)) + '\n')
                    else:
                        db.execute('INSERT INTO distinct_texts VALUES (?,?,?)', (text_hash, number, rid))
        report['distinct_nonempty_texts'] = db.execute('SELECT COUNT(DISTINCT text_sha256) FROM distinct_texts').fetchone()[0]
        report['parsed_rows_sha256'] = parsed.hexdigest()
        checks = []
        expected = dict(total_rows=660622, file_bytes=97400616, empty_review_text_count=13,
                        missing_app_version_count=159701, duplicate_review_id_count=0,
                        distinct_nonempty_texts=484189, file_sha256=expected_file['sha256'],
                        rating_distribution=manifest['reviews_by_rating'],
                        rows_per_calendar_month=manifest['reviews_by_month'],
                        min_timestamp=manifest['window']['first_review'],
                        max_timestamp=manifest['window']['last_review'])
        if input_path.name != manifest['assignment_scope']['final_input']:
            expected = {}
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        if expected:
            print('Check | Actual | Expected | Result')
            print('--- | --- | --- | ---')
        for key, value in expected.items():
            result = 'PASS' if report[key] == value else 'FAIL'
            checks.append(dict(check=key, actual=report[key], expected=value, result=result))
            print(f'{key} | {report[key]} | {value} | {result}')
        report['checks'] = checks
        report['all_checks_passed'] = all(check['result'] == 'PASS' for check in checks)
        write_json(run / 'ingestion_report.json', report)
        write_json(run / 'data_manifest.json', dict(report, input_path=str(input_path.resolve()),
                   source_fields=list(FIELDS), row_hash_rule='SHA-256 of canonical UTF-8 JSON array of six exact source strings',
                   text_hash_rule='SHA-256 of exact review_text UTF-8 bytes; no normalization',
                   distinct_texts_layout='One row per nonempty source row; group by text_sha256 to obtain review_ids',
                   expected_manifest_sha256=file_sha(ROOT / 'data' / 'manifest.json')))
        if not report['all_checks_passed']:
            raise SystemExit(1)
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    prepare(args.input, args.run_id)


if __name__ == '__main__':
    main()
