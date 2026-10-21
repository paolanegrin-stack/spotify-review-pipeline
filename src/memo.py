"""Claim-grounded memo generation; dry-run never constructs an API client."""

import argparse
from contextlib import closing
import csv
from decimal import Decimal
import json
import re
import sqlite3
import sys

from . import config as C
from .enrich import object_schema
from .prepare import canonical, file_sha, ROOT
from .rank import FIELDS as RANK_FIELDS, membership, rank_rows
from .stage_common import (atomic_text, cache_key, cached, completed_records, csv_text,
                           live_stage, preview, request, save_cache, send, summary)
from .verify import compare

CLAIM_FIELDS = ('claim_id', 'issue_id', 'metric', 'value')
METRICS = ('complaint_count', 'severity_sum', 'mean_severity', 'priority_score')
UUID = re.compile(r'\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b')
NUMBER = re.compile(r'(?<![\w.])[-+]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)(?:[eE][-+]?\d+)?(?!\w|\.\d)')


def make_claims(ranking):
    return [dict(claim_id=f'C{i * len(METRICS) + j}', issue_id=row['issue_id'], metric=metric, value=str(row[metric]))
            for i, row in enumerate(ranking) for j, metric in enumerate(METRICS, 1)]


def citation_data(pack):
    totals = pack['run_totals']
    agreement = (pack.get('verifier_agreement') or {}).get('agreement',{}).get('topic')
    values = [('T1','rows',totals['rows'],'total rows'),
              ('T2','completed',totals['completed'],'completed records'),
              ('T3','quarantined',totals['quarantined'],'quarantined records'),
              ('T4','needs_review',totals['needs_review'],'records needing review'),
              ('T5','verifier_topic_agreement',agreement,'verifier topic agreement'),
              ('T6','unresolved',totals['unresolved'],'unresolved records')]
    total_claims = [dict(claim_id=cid,metric=metric,value=None if value is None else str(value))
                    for cid,metric,value,_ in values]
    snippets = [dict(claim_id=cid,snippet=f'{value} {label} [{cid}]')
                for cid,_,value,label in values if value is not None]
    templates = dict(complaint_count='{value} complaints [{id}]',severity_sum='severity sum {value} [{id}]',
                     mean_severity='mean severity {value} [{id}]',priority_score='priority score {value} [{id}]')
    snippets = [dict(claim_id=c['claim_id'],issue_id=c['issue_id'],
                     snippet=templates[c['metric']].format(value=c['value'],id=c['claim_id']))
                for c in pack['claims']] + snippets
    return total_claims,snippets


def build_pack(path):
    folder = path.parent
    with (folder / 'ranking.csv').open(encoding='utf-8', newline='') as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(RANK_FIELDS):
            raise ValueError('Incorrect ranking columns')
        ranking = list(reader)
    if len(ranking) > C.MEMO_MAX_ISSUES:
        raise ValueError('Evidence pack exceeds configured issue bound')
    records = completed_records(path)
    expected = [{k: str(v) for k, v in row.items()} for row in rank_rows(records, membership(records))]
    if ranking != expected:
        raise ValueError('Ranking is stale or inconsistent with saved classifications')
    claims = make_claims(ranking)
    atomic_text(folder / 'claims.csv', csv_text(CLAIM_FIELDS, claims))
    issues_file = folder / 'issues.json'
    saved_issues = json.loads(issues_file.read_text(encoding='utf-8')) if issues_file.exists() else []
    if not isinstance(saved_issues, list):
        raise ValueError('issues.json must contain an array')
    by_issue = {row['issue_id']: row for row in saved_issues}
    if len(by_issue) != len(saved_issues):
        raise ValueError('Duplicate issue descriptions')
    by_id = {row['review_id']: row for row in records}
    issues, quotes, missing_names = [], [], []
    for row in ranking:
        issue = by_issue.get(row['issue_id'])
        if issue is None:
            missing_names.append(row['issue_id'])
            continue
        if issue.get('member_count') != int(row['complaint_count']):
            raise ValueError('Stale issue member count: ' + row['issue_id'])
        if (not isinstance(issue.get('name'), str) or not issue['name'].strip()
                or len(issue['name']) > C.GROUP_MAX_NAME_CHARS
                or not isinstance(issue.get('description'), str)
                or len(issue['description']) > C.GROUP_MAX_DESCRIPTION_CHARS):
            raise ValueError('Invalid or unbounded saved issue description')
        issues.append(dict(issue_id=row['issue_id'], name=issue['name'], description=issue['description']))
        reps = issue.get('representative_ids', [])
        if not isinstance(reps, list) or any(not isinstance(rid, str) for rid in reps) or len(set(reps)) != len(reps):
            raise ValueError('Invalid representative IDs')
        for rid in reps[:C.MEMO_MAX_REPRESENTATIVES]:
            source = by_id.get(rid)
            if source is None or row['issue_id'] != 'ISS-' + source['labels']['topic'].upper() or source['labels']['intent'] not in ('complaint', 'cancellation'):
                raise ValueError('Representative is not a saved member of the issue')
            quote = source['labels']['evidence_quote']
            if not isinstance(quote, str) or not quote.strip() or quote not in source['review_text'] or len(quote) > max(C.SHORT_TEXT_CHARS, C.MAX_QUOTE_CHARS):
                raise ValueError('Invalid representative quote')
            quotes.append(dict(issue_id=row['issue_id'], review_id=rid, evidence_quote=quote))
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        statuses = dict(db.execute('SELECT status,COUNT(*) FROM records GROUP BY status'))
    total = sum(statuses.values())
    totals = dict(rows=total, completed=len(records), quarantined=statuses.get('quarantined', 0),
                  unresolved=total-len(records)-statuses.get('quarantined', 0),
                  needs_review=sum(row['labels']['needs_review'] for row in records))
    agreement = None
    prediction_file = folder / 'verify' / 'verifier_predictions.jsonl'
    sample_file = folder / 'verify' / 'sample.json'
    if prediction_file.exists() and sample_file.exists():
        sample = json.loads(sample_file.read_text(encoding='utf-8'))
        predictions = [json.loads(line) for line in prediction_file.read_text(encoding='utf-8').splitlines() if line.strip()]
        predictions = [p for p in predictions if p.get('label_config') == C.VERIFY_LABEL_CONFIG
                       and p.get('review_id') in by_id and p.get('source_sha256') == by_id[p['review_id']]['source_sha256']]
        if sample.get('enrichment_label_config') == C.LABEL_CONFIG:
            selected = [by_id[rid] for rid in sample['review_ids'] if rid in by_id]
            measured = compare(selected, predictions)
            agreement = {k: v for k, v in measured.items() if k != 'disagreements'}
    artifacts = {str(p.relative_to(folder)): file_sha(p) for p in
                 (folder/'ranking.csv',folder/'claims.csv',issues_file,prediction_file,sample_file) if p.is_file()}
    pack = dict(ranking=ranking, claims=claims, issues=issues, representative_quotes=quotes,
                run_totals=totals, verifier_agreement=agreement,
                missing_issue_descriptions=missing_names, input_artifacts=artifacts)
    pack['total_claims'],pack['citation_snippets'] = citation_data(pack)
    return pack


def eligible(pack):
    return [r['issue_id'] for r in pack['ranking']
            if r['issue_id'] in {'ISS-' + topic.upper() for topic in C.MEMO_DECISION_TOPICS}]


def build_request(pack, errors=None):
    pack = dict(pack)
    pack['total_claims'],pack['citation_snippets'] = citation_data(pack)
    ids = [r['issue_id'] for r in pack['ranking']]
    # Empty packs can be previewed but are explicitly blocked from live execution.
    issue_schema = dict(type='string', **(dict(enum=ids) if ids else {}))
    schema = object_schema(dict(memo_markdown=dict(type='string', minLength=1, maxLength=C.MEMO_MAX_MARKDOWN_CHARS),
                                recommended_issue_id=issue_schema,
                                alternatives=dict(type='array', minItems=2, maxItems=C.MEMO_MAX_ISSUES, items=issue_schema),
                                cited_claim_ids=dict(type='array', items=dict(type='string')),
                                cited_review_ids=dict(type='array', items=dict(type='string'))))
    user = dict(evidence_pack=pack)
    if errors:
        user['validation_error'] = errors
    return request('memo', user, schema)


def numeric_values(value):
    if isinstance(value, dict):
        return set().union(*(numeric_values(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(numeric_values(v) for v in value)) if value else set()
    if type(value) in (int, float):
        return {Decimal(str(value))}
    return set()


def check(payload, pack):
    problems = []
    keys = {'memo_markdown','recommended_issue_id','alternatives','cited_claim_ids','cited_review_ids'}
    if not isinstance(payload, dict) or set(payload) != keys:
        return ['Incorrect memo output keys']
    text = payload['memo_markdown']
    if not isinstance(text, str) or not text.strip() or len(text) > C.MEMO_MAX_MARKDOWN_CHARS:
        return ['Invalid memo_markdown']
    total_claims,snippets = citation_data(pack)
    claims = {r['claim_id']: r for r in [*pack['claims'],*total_claims]}
    issue_ids = {r['issue_id'] for r in pack['ranking']}
    review_ids = {r['review_id'] for r in pack['representative_quotes']}
    recommendation = payload['recommended_issue_id']
    if not isinstance(recommendation, str) or recommendation not in eligible(pack):
        problems.append('Unknown or ineligible recommended issue ID')
    alternatives = payload['alternatives']
    if (not isinstance(alternatives, list) or any(not isinstance(x,str) or x not in eligible(pack) for x in alternatives)
            or len(alternatives) < 2 or len(alternatives) != len(set(alternatives)) or recommendation in alternatives):
        problems.append('Need at least two distinct eligible alternatives, excluding recommendation')
        alternatives = []
    declared = {}
    for field, allowed in (('cited_claim_ids',set(claims)),('cited_review_ids',review_ids)):
        value = payload[field]
        if not isinstance(value,list) or any(not isinstance(x,str) or x not in allowed for x in value) or len(value) != len(set(value)):
            problems.append('Unknown or duplicate ID in ' + field)
            declared[field] = set()
        else:
            declared[field] = set(value)
    cited_claims, cited_reviews = set(), set()
    for bracket in re.findall(r'\[([^\]\n]+)\]', text):
        for token in re.split(r'[,;\s]+',bracket.strip()):
            if token in claims:
                cited_claims.add(token)
            elif token in review_ids:
                cited_reviews.add(token)
            elif token not in issue_ids:
                problems.append('Unknown citation ID: ' + token)
    for field, found in (('cited_claim_ids',cited_claims),('cited_review_ids',cited_reviews)):
        if declared[field] != found:
            problems.append(field + ' does not match bracket citations in memo')
    if review_ids and not cited_reviews:
        problems.append('Memo must cite at least one supplied representative review ID')
    mentioned_issues = set(re.findall(r'\bISS-[A-Z][A-Z0-9_-]*',text))
    for iid in mentioned_issues - issue_ids:
        problems.append('Unknown issue ID in memo: ' + iid)
    for iid in [recommendation, *alternatives]:
        if isinstance(iid,str) and iid not in mentioned_issues:
            problems.append('Recommended/alternative issue missing from memo text: ' + iid)
    for rid in UUID.findall(text):
        if rid not in review_ids:
            problems.append('Unknown review ID in memo: ' + rid)
    allowed_values = {Decimal(r['value']) for r in claims.values() if r['value'] is not None}
    if '%' in text or re.search(r'\bpercent(?:age)?s?\b',text,re.I):
        problems.append('Percentages are not allowed; copy the supplied snippet verbatim')
    for line in text.splitlines():
        scrubbed = line
        for snippet in sorted((r['snippet'] for r in snippets),key=len,reverse=True):
            scrubbed = scrubbed.replace(snippet,' ')
        identifiers = list(issue_ids | review_ids) + re.findall(r'\b[CT]\d+\b',line) + UUID.findall(line)
        for identifier in sorted(set(identifiers), key=len, reverse=True):
            scrubbed = re.sub(r'(?<![\w-])' + re.escape(identifier) + r'(?![\w-])', ' ', scrubbed)
        for match in NUMBER.finditer(scrubbed):
            literal = match.group()
            value = Decimal(literal.replace(',',''))
            if value not in allowed_values:
                problems.append('Invented numeric value: ' + literal)
            problems.append('Number must be copied with its complete citation snippet: ' + literal)
        if re.search(r'\d\s*(?:million|billion|thousand|[kKmMbB])\b', scrubbed):
            problems.append('Unsupported numeric magnitude abbreviation')
    return list(dict.fromkeys(problems))


def save_memo(path, payload, problems):
    body = payload.get('memo_markdown','') if isinstance(payload,dict) else ''
    if not isinstance(body,str):
        body = str(body)
    header = '# FAILED CHECKS\n\n' + '\n'.join('- ' + p for p in problems) + '\n\n' if problems else ''
    atomic_text(path.parent/'memo.md',header+body+'\n')
    atomic_text(path.parent/'memo_check.json',json.dumps(dict(passed=not problems, problems=problems,
                label_config=C.MEMO_LABEL_CONFIG, output=payload),ensure_ascii=False,indent=2)+'\n')


def run(args):
    with summary(args,'memo') as path:
        args.stage_total, args.stage_completed = 1, 0
        pack = build_pack(path)
        value = build_request(pack)
        if args.dry_run:
            if len(eligible(pack)) < 3 or pack['missing_issue_descriptions']:
                print('Preview only: insufficient observed alternatives or saved issue descriptions for a live memo.')
            preview(value)
            return
        if len(eligible(pack)) < 3 or pack['missing_issue_descriptions']:
            args.stage_blocked = True
            print('Memo blocked: need three observed eligible issues and their saved descriptions; no evidence will be invented.')
            return
        key = cache_key('memo',value,[])
        saved = cached(path,'memo',key)
        if saved is not None and not check(saved,pack):
            save_memo(path,saved,[])
            args.stage_completed = 1
            print('Memo reused from validated request cache.')
            return
        if Decimal(args.max_spend_usd) == 0:
            args.spend_stopped = True
            print('Spend limit stopped memo: zero budget; no calls.')
            return
        with live_stage(path,'memo',args.max_spend_usd) as ledger:
            ledger.input_artifacts = pack['input_artifacts']
            problems, holder = [], {}
            for attempt in range(C.MEMO_VALIDATION_RETRIES+1):
                holder.clear()
                def validate(candidate):
                    holder['candidate'] = candidate
                    errors = check(candidate,pack)
                    holder['errors'] = errors
                    if errors:
                        raise ValueError('; '.join(errors))
                    return candidate
                from dotenv import load_dotenv
                from openai import OpenAI
                load_dotenv(ROOT/'.env')
                try:
                    with OpenAI(max_retries=0,timeout=C.TIMEOUT_SECONDS) as client:
                        result = send(build_request(pack,problems),[],ledger,args.phase,validate,client)
                except ValueError as exc:
                    result = holder.get('candidate',{})
                    problems = holder.get('errors') or [str(exc)]
                    save_memo(path,result,problems)
                    print(f'Memo failed checks on attempt {attempt+1}: ' + '; '.join(problems))
                    continue
                save_cache(path,'memo',key,result)
                save_memo(path,result,[])
                args.stage_completed = 1
                print('Memo saved; all deterministic checks passed.')
                return
            args.stage_failed = True
            print(f'Memo saved as FAILED CHECKS after {C.MEMO_VALIDATION_RETRIES} validation retries.')


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id',required=True)
    parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('--max-spend-usd',default=C.DEFAULT_MAX_SPEND_USD)
    parser.add_argument('--phase',choices=('initial','resume'),default='initial')
    run(parser.parse_args())


if __name__ == '__main__':
    main()
