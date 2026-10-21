"""Code assigns membership; the model names issues from bounded saved evidence only."""

import argparse
from decimal import Decimal
import json
import sys

from . import config as C
from .enrich import object_schema
from .rank import membership
from .stage_common import (atomic_text, cache_key, cached, completed_records, csv_text,
                           live_stage, preview, request, save_cache, send, summary)


def evidence(records):
    by_id = {row['review_id']: row for row in records}
    issues = {}
    for pair in membership(records):
        issues.setdefault(pair['issue_id'], []).append(by_id[pair['review_id']])
    return [(issue, rows, rows[:C.GROUP_MAX_QUOTES]) for issue, rows in sorted(issues.items())]


def build_request(issue_id, sample):
    if not 1 <= len(sample) <= C.GROUP_MAX_QUOTES or C.GROUP_MAX_QUOTES > 30:
        raise ValueError('Issue sample must contain 1-30 evidence quotes')
    ids = [row['review_id'] for row in sample]
    schema = object_schema(dict(issue_id=dict(type='string', enum=[issue_id]),
                                issue_name=dict(type='string', minLength=1, maxLength=C.GROUP_MAX_NAME_CHARS),
                                description=dict(type='string', minLength=1, maxLength=C.GROUP_MAX_DESCRIPTION_CHARS),
                                representative_review_ids=dict(type='array', maxItems=C.GROUP_MAX_REPRESENTATIVES,
                                                               items=dict(type='string', enum=ids))))
    user = dict(issue_id=issue_id, evidence=[dict(review_id=r['review_id'], evidence_quote=r['labels']['evidence_quote']) for r in sample])
    return request('group', user, schema)


def validate(payload, issue_id, sample):
    keys = {'issue_id', 'issue_name', 'description', 'representative_review_ids'}
    if not isinstance(payload, dict) or set(payload) != keys or payload['issue_id'] != issue_id:
        raise ValueError('Incorrect issue keys or issue ID')
    for field, maximum in (('issue_name', C.GROUP_MAX_NAME_CHARS), ('description', C.GROUP_MAX_DESCRIPTION_CHARS)):
        value = payload[field]
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError('Invalid ' + field)
    # Enforce one sentence conservatively, allowing decimal points in numbers.
    description = payload['description'].strip()
    import re
    if '\n' in description or len(re.findall(r'[.!?](?:\s|$)', description)) > 1:
        raise ValueError('Description must be one sentence')
    ids = payload['representative_review_ids']
    allowed = {r['review_id'] for r in sample}
    if (not isinstance(ids, list) or len(ids) > C.GROUP_MAX_REPRESENTATIVES
            or any(not isinstance(rid, str) or rid not in allowed for rid in ids)
            or len(ids) != len(set(ids))):
        raise ValueError('Foreign, duplicate, or excessive representative review IDs')
    return dict(issue_id=issue_id, name=payload['issue_name'], description=payload['description'], representative_ids=ids)


def run(args):
    with summary(args, 'group') as path:
        records = completed_records(path)
        issues = evidence(records)
        args.stage_total = len(issues)
        args.stage_completed = 0
        if args.dry_run:
            if issues:
                preview(build_request(issues[0][0], issues[0][2]))
            else:
                print('No completed complaints/cancellations to group.')
            return
        pairs = membership(records)
        atomic_text(path.parent / 'membership.csv', csv_text(('issue_id', 'review_id'), pairs))
        # Invalidate any prior export before processing a changed membership set.
        # Durable cached names can be re-exported without another model call.
        atomic_text(path.parent / 'issues.json', '[]\n')
        if not issues:
            atomic_text(path.parent / 'issues.json', '[]\n')
            print('No completed complaints/cancellations to group; empty membership and issues saved.')
            return
        if Decimal(args.max_spend_usd) == 0:
            args.spend_stopped = True
            print('Spend limit stopped group naming: zero budget; code-only membership saved; no calls.')
            return
        output = []
        with live_stage(path, 'group', args.max_spend_usd) as ledger:
            for issue_id, members, sample in issues:
                value = build_request(issue_id, sample)
                key = cache_key('group', value, sample)
                result = cached(path, 'group', key)
                if result is None:
                    from dotenv import load_dotenv
                    from openai import OpenAI
                    from .prepare import ROOT
                    load_dotenv(ROOT / '.env')
                    with OpenAI(max_retries=0, timeout=C.TIMEOUT_SECONDS) as client:
                        result = send(value, sample, ledger, args.phase, lambda p: validate(p, issue_id, sample), client)
                    save_cache(path, 'group', key, result)
                else:
                    result = validate(dict(issue_id=result['issue_id'], issue_name=result['name'],
                                           description=result['description'], representative_review_ids=result['representative_ids']), issue_id, sample)
                output.append(dict(result, member_count=len(members)))
                args.stage_completed = len(output)
                atomic_text(path.parent / 'issues.json', json.dumps(output, ensure_ascii=False, indent=2) + '\n')
                print(f'Issue names saved: {len(output)}/{len(issues)}; membership remains code-defined.')


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--phase', choices=('initial', 'resume'), default='initial')
    parser.add_argument('--max-spend-usd', default=C.DEFAULT_MAX_SPEND_USD)
    run(parser.parse_args())


if __name__ == '__main__':
    main()
