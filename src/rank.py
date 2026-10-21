"""Deterministic baseline ranking; no API client or network access."""

import argparse
import csv
from decimal import Decimal, ROUND_HALF_UP, localcontext
import time

from . import config as C
from .stage_common import run_path, completed_records, atomic_text, csv_text

FIELDS = ('rank', 'issue_id', 'complaint_count', 'severity_sum', 'mean_severity', 'priority_score')


def mean_severity(total, count):
    if count <= 0:
        raise ValueError('Issue must have at least one member')
    with localcontext() as context:
        context.prec = max(28, len(str(total)) + len(str(count)) + 12)
        return format((Decimal(total) / Decimal(count)).quantize(Decimal(C.RANK_MEAN_QUANTUM), rounding=ROUND_HALF_UP), 'f')


def membership(records):
    pairs = []
    for row in records:
        labels = row['labels']
        if labels['intent'] in ('complaint', 'cancellation'):
            if labels['topic'] not in C.TOPICS:
                raise ValueError('Invalid topic in completed record')
            pairs.append(dict(issue_id='ISS-' + labels['topic'].upper(), review_id=row['review_id']))
    pairs.sort(key=lambda row: (row['issue_id'], row['review_id']))
    if len({p['review_id'] for p in pairs}) != len(pairs):
        raise ValueError('Duplicate membership IDs')
    return pairs


def rank_rows(records, pairs):
    expected = membership(records)
    if sorted(pairs, key=lambda r: (r['issue_id'], r['review_id'])) != expected:
        raise ValueError('Membership must contain every completed complaint/cancellation exactly once, by topic')
    by_id = {row['review_id']: row for row in records}
    grouped = {}
    for pair in pairs:
        severity = by_id[pair['review_id']]['labels']['severity']
        if type(severity) is not int or not 1 <= severity <= 5:
            raise ValueError('Invalid severity')
        values = grouped.setdefault(pair['issue_id'], [])
        values.append(severity)
    rows = []
    for issue, values in grouped.items():
        total = sum(values)
        rows.append(dict(issue_id=issue, complaint_count=len(values), severity_sum=total,
                         mean_severity=mean_severity(total, len(values)), priority_score=total))
    rows.sort(key=lambda r: (-r['priority_score'], r['issue_id']))
    return [dict(rank=i, **row) for i, row in enumerate(rows, 1)]


def execute(run_id):
    path = run_path(run_id)
    records = completed_records(path)
    member_path = path.parent / 'membership.csv'
    if member_path.exists():
        with member_path.open(encoding='utf-8', newline='') as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != ['issue_id', 'review_id']:
                raise ValueError('Incorrect membership columns')
            pairs = list(reader)
    else:
        pairs = membership(records)
        atomic_text(member_path, csv_text(('issue_id', 'review_id'), pairs))
        print('Created code-only membership.csv; no issue naming call is needed for ranking.')
    rows = rank_rows(records, pairs)
    atomic_text(path.parent / 'ranking.csv', csv_text(FIELDS, rows))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    started = time.monotonic()
    try:
        rows = execute(args.run_id)
        print(csv_text(FIELDS, rows), end='')
    finally:
        print(f'Final summary | run id: {args.run_id} | stage: rank | calls=0 | cost USD=$0.00000000 | wall-clock seconds={time.monotonic()-started:.3f}')


if __name__ == '__main__':
    main()
