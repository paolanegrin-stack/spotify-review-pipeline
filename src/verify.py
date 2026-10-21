"""Independent verification on a declared reproducible sample. No calls in dry-run/test mode."""

import argparse
from copy import deepcopy
from decimal import Decimal
import json
import random
import sys

from . import config as C
from .prepare import canonical
from .enrich import object_schema
from .stage_common import (atomic_text, cache_key, cached, completed_records, digest,
                           live_stage, preview, request, save_cache, send, summary)

FIELDS = ('topic', 'intent', 'severity', 'sentiment', 'needs_review')


def select_sample(records, size):
    if size <= 0:
        raise ValueError('sample-size must be positive')
    population = sorted(records, key=lambda r: r['review_id'])
    sample = random.Random(C.VERIFY_SAMPLE_SEED).sample(population, min(size, len(population)))
    declaration = dict(seed=C.VERIFY_SAMPLE_SEED, requested_size=size, actual_size=len(sample),
                       eligible_completed=len(population), enrichment_label_config=C.LABEL_CONFIG,
                       verifier_label_config=C.VERIFY_LABEL_CONFIG,
                       population_sha256=digest([(r['review_id'], r['source_sha256']) for r in population]),
                       review_ids=[r['review_id'] for r in sample])
    return sample, declaration


def build_request(rows):
    if not 1 <= len(rows) <= min(C.VERIFY_BATCH_SIZE, C.MAX_BATCH_SIZE, 50):
        raise ValueError('Invalid verifier batch size')
    local = {f'r{i}': row for i, row in enumerate(rows, 1)}
    properties = dict(id=dict(type='string', enum=list(local)),
                      topic=dict(type='string', enum=list(C.TOPICS)),
                      intent=dict(type='string', enum=list(C.INTENTS)),
                      severity=dict(type='integer', minimum=1, maximum=5),
                      sentiment=dict(type='number', enum=list(C.SENTIMENTS)), needs_review=dict(type='boolean'))
    schema = object_schema(dict(records=dict(type='array', minItems=len(rows), maxItems=len(rows), items=object_schema(properties))))
    # Explicit allow-list prevents enrichment labels, metadata, or real IDs leaking.
    user = dict(reviews=[dict(id=key, review_text=row['review_text']) for key, row in local.items()])
    return request('verify', user, schema), local


def validate(payload, local):
    if not isinstance(payload, dict) or set(payload) != {'records'} or not isinstance(payload['records'], list):
        raise ValueError('Expected records array')
    output = {}
    for row in payload['records']:
        if not isinstance(row, dict) or set(row) != {'id', *FIELDS}:
            raise ValueError('Incorrect verifier keys')
        key = row['id']
        if not isinstance(key, str) or key not in local or key in output:
            raise ValueError('Foreign or duplicate verifier ID')
        if row['topic'] not in C.TOPICS or row['intent'] not in C.INTENTS:
            raise ValueError('Invalid verifier label')
        if type(row['severity']) is not int or not 1 <= row['severity'] <= 5:
            raise ValueError('Invalid verifier severity')
        if type(row['sentiment']) not in (int, float) or row['sentiment'] not in C.SENTIMENTS:
            raise ValueError('Invalid verifier sentiment')
        if type(row['needs_review']) is not bool:
            raise ValueError('Invalid verifier needs_review')
        output[key] = {field: row[field] for field in FIELDS}
    if set(output) != set(local):
        raise ValueError('Missing verifier ID')
    return output


def compare_record(enriched, verified):
    return {field: dict(enricher=enriched[field], verifier=verified[field])
            for field in FIELDS if enriched[field] != verified[field]}


def compare(records, predictions):
    by_id = {row['review_id']: row for row in predictions}
    pairs = [(row, by_id[row['review_id']]) for row in records if row['review_id'] in by_id]
    n = len(pairs)
    rates = {field: sum(row['labels'][field] == pred[field] for row, pred in pairs) / n if n else None
             for field in FIELDS}
    mae = sum(abs(row['labels']['severity'] - pred['severity']) for row, pred in pairs) / n if n else None
    disagreements = []
    for row, pred in pairs:
        differences = compare_record(row['labels'], pred)
        if differences:
            disagreements.append(dict(review_id=row['review_id'], review_text=row['review_text'], differences=differences))
    return dict(compared=n, selected=len(records), missing=len(records)-n,
                agreement=rates, severity_mean_absolute_error=mae, disagreements=disagreements)


def planted_error(record, prediction):
    test_copy = deepcopy(record)
    test_copy['labels']['topic'] = next(topic for topic in C.TOPICS
                                      if topic not in (prediction['topic'], record['labels']['topic']))
    differences = compare_record(test_copy['labels'], prediction)
    return dict(synthetic=True, business_tables_modified=False, review_id=record['review_id'],
                planted_topic=test_copy['labels']['topic'], verifier_topic=prediction['topic'],
                flagged='topic' in differences, differences=differences)


def export(path, records, predictions, declaration):
    folder = path.parent / 'verify'
    atomic_text(folder / 'sample.json', json.dumps(declaration, indent=2, ensure_ascii=False) + '\n')
    atomic_text(folder / 'verifier_predictions.jsonl', ''.join(canonical(p) + '\n' for p in predictions))
    result = compare(records, predictions)
    lines = ['# Independent verifier disagreements', '',
             f'Seed: {declaration["seed"]}; requested: {declaration["requested_size"]}; selected: {len(records)};',
             f'compared: {result["compared"]}; missing predictions: {result["missing"]}.',
             'Agreement is measured only over saved predictions; missing cases are explicitly excluded.', '',
             '| Field | Agreement |', '|---|---|']
    for field, rate in result['agreement'].items():
        lines.append(f'| {field} | {rate:.6f} |' if rate is not None else f'| {field} | unavailable |')
    mae = result['severity_mean_absolute_error']
    lines += ['', f'Severity mean absolute error: {mae:.6f}' if mae is not None else 'Severity mean absolute error: unavailable', '',
              '## Disagreements', '']
    for row in result['disagreements']:
        lines += [f'### {row["review_id"]}', '', '```json', json.dumps(row, ensure_ascii=False, indent=2), '```', '']
    if not result['disagreements']:
        lines.append('None among compared records.' if result['compared'] else 'No verifier predictions saved yet; agreement is unavailable.')
    atomic_text(folder / 'disagreement_report.md', '\n'.join(lines) + '\n')


def run(args):
    with summary(args, 'verify') as path:
        records = completed_records(path)
        if args.planted_error_test:
            saved = path.parent / 'verify' / 'verifier_predictions.jsonl'
            if not saved.exists():
                raise ValueError('No saved verifier predictions. Planted-error test never calls the API.')
            by_id = {r['review_id']: r for r in records}
            for line in saved.read_text(encoding='utf-8').splitlines():
                prediction = json.loads(line)
                if prediction['review_id'] in by_id:
                    print(json.dumps(planted_error(by_id[prediction['review_id']], prediction), indent=2, ensure_ascii=False))
                    return
            raise ValueError('No saved verifier prediction matches a completed record')
        selected, declaration = select_sample(records, args.sample_size)
        args.stage_total = len(selected)
        args.stage_completed = 0
        print('Declared verification sample: ' + canonical(declaration))
        if not selected:
            print('No completed records available for verification.')
            return
        if args.dry_run:
            preview(build_request(selected[:C.VERIFY_BATCH_SIZE])[0])
            return
        if Decimal(args.max_spend_usd) == 0:
            args.spend_stopped = True
            print('Spend limit stopped verify: zero budget; no calls.')
            return
        predictions = []
        with live_stage(path, 'verify', args.max_spend_usd) as ledger:
            # No credentials/client for cached batches; each batch persists atomically.
            for i in range(0, len(selected), C.VERIFY_BATCH_SIZE):
                rows = selected[i:i+C.VERIFY_BATCH_SIZE]
                value, local = build_request(rows)
                key = cache_key('verify', value, rows)
                result = cached(path, 'verify', key)
                if result is None:
                    from dotenv import load_dotenv
                    from openai import OpenAI
                    from .prepare import ROOT
                    load_dotenv(ROOT / '.env')
                    with OpenAI(max_retries=0, timeout=C.TIMEOUT_SECONDS) as client:
                        result = send(value, rows, ledger, args.phase, lambda p: validate(p, local), client)
                    save_cache(path, 'verify', key, result)
                else:
                    result = validate(dict(records=[dict(id=k, **v) for k, v in result.items()]), local)
                predictions.extend(dict(review_id=local[k]['review_id'], source_sha256=local[k]['source_sha256'],
                                        label_config=C.VERIFY_LABEL_CONFIG, **v) for k, v in result.items())
                args.stage_completed = len(predictions)
                export(path, selected, predictions, declaration)
                print(f'Verifier predictions saved: {len(predictions)}/{len(selected)}')


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--sample-size', type=int, default=C.VERIFY_PILOT_SAMPLE_SIZE)
    parser.add_argument('--planted-error-test', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--phase', choices=('initial', 'resume'), default='initial')
    parser.add_argument('--max-spend-usd', default=C.DEFAULT_MAX_SPEND_USD)
    run(parser.parse_args())


if __name__ == '__main__':
    main()
