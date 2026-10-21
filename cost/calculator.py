"""Offline pilot replay. Imports use no credentials, network, or model clients."""

import argparse
from contextlib import closing
import csv
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
ROLES = ('enrich', 'verify', 'group', 'memo')
ITEMS = ('input', 'cached_input', 'output')
USAGE_FIELDS = ('run_id','invocation_id','pilot_phase','request_id','role','model','item','units',
                'price_usd_per_million_tokens','cost_usd','source_url','checked_date')


def lines(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def load_rates(path):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        return {(r['provider'],r['model'],r['item']):r for r in csv.DictReader(stream)}


def bill(call, rates):
    inp,cached,out = (call.get(k) for k in ('input_tokens','cached_input_tokens','output_tokens'))
    valid = all(type(v) is int and v >= 0 for v in (inp,cached,out)) and cached <= inp
    units = dict(input=inp-cached,cached_input=cached,output=out) if valid else dict.fromkeys(ITEMS)
    output = []
    for item in ITEMS:
        rate = rates.get(('openai',call['model'],item),{})
        price = rate.get('price_usd_per_million_tokens','')
        cost = Decimal(units[item])*Decimal(price)/Decimal(1000000) if units[item] is not None and price else None
        output.append(dict(request_id=call['request_id'],role=call['role'],model=call['model'],item=item,
                           units=units[item],price_usd_per_million_tokens=price,
                           cost_usd=cost,source_url=rate.get('source_url',''),checked_date=rate.get('checked_date','')))
    return output


def total(values):
    values = list(values)
    return None if any(v is None for v in values) else sum(values,Decimal(0))


def records(folder, label_config):
    with closing(sqlite3.connect((folder/'pipeline.db').resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute('''SELECT r.*,e.result_json,e.cache_source_id FROM records r
            LEFT JOIN enrichment_results e ON r.review_id=e.review_id AND e.label_config=? ORDER BY r.source_row''',(label_config,))
        return [dict(row) for row in rows]


def measure(calls, stages, rates, source, summary):
    result = {}
    for role in ROLES:
        attempted = [c for c in calls if c['role']==role]
        charges = [r for c in attempted for r in bill(c,rates)]
        ids = {rid for c in attempted if c['outcome']=='succeeded' for rid in c['review_ids']}
        if role=='enrich':
            work = len({r['text_sha256'] for r in source if r['review_id'] in ids and r['status']=='completed'})
        elif role=='verify':
            work = len(ids)
        elif role=='group':
            work = len({json.loads(c['request']['input'])['issue_id'] for c in attempted if c['outcome']=='succeeded'})
        else:
            work = int(any(c['outcome']=='succeeded' for c in attempted))
        config = summary.get('config',{})
        prefix = '' if role=='enrich' else role.upper()+'_'
        result[role] = dict(calls=len(attempted),succeeded=sum(c['outcome']=='succeeded' for c in attempted),
                            failed=sum(c['outcome']=='failed' for c in attempted),work=work,
                            cost=total(r['cost_usd'] for r in charges),
                            seconds=sum(s['wall_clock_seconds'] for s in stages if s['stage']==role),
                            tokens={k:total(c.get(k) for c in attempted) for k in
                                    ('input_tokens','cached_input_tokens','output_tokens','reasoning_tokens')},
                            model=summary.get('model_ids',{}).get(role,config.get(prefix+'MODEL')),
                            effort=config.get(prefix+'EFFORT'),prompt=summary.get('prompt_versions',{}).get(role),
                            batch=max((len(c['review_ids']) for c in attempted),default=config.get(prefix+'BATCH_SIZE','one aggregate pack')),
                            workers=summary.get('workers',1) if role=='enrich' else 1)
    for stage in ('prepare','rank'):
        result[stage] = dict(seconds=sum(s['wall_clock_seconds'] for s in stages if s['stage']==stage))
    return result


def estimates(measured, rows, workers, reuse=True, multiplier=Decimal('1')):
    target = dict(enrich=484189 if reuse else 660609,verify=2000,group=1,memo=1)
    costs,times = [],[]
    for role in ROLES:
        m = measured[role]
        factor = (Decimal(target[role])/Decimal(m['work']) if m['work'] else None) if role in ('enrich','verify') else (Decimal(1) if m['work'] else None)
        costs.append(m['cost']*factor*multiplier if factor is not None and m['cost'] is not None else None)
        seconds = Decimal(str(m['seconds']))*factor*multiplier if factor is not None else None
        if seconds is not None and role=='enrich':
            seconds /= workers
        times.append(seconds)
    local = sum((Decimal(str(measured[stage]['seconds']))*Decimal(target_rows)/rows
                 for stage,target_rows in (('prepare',660622),('rank',660609))),Decimal(0))
    duration = total(times)
    return dict(api_cost=total(costs),seconds=None if duration is None else duration+local)


def display(value):
    return 'UNKNOWN' if value is None else str(value)


def run(run_ids, budget, workers):
    rates = load_rates(ROOT/'cost'/'rates.csv')
    report = ['# Pilot cost and runtime replay','',
              'Offline only. USD API costs use saved usage and editable rates.csv; infrastructure/local-compute cost is UNKNOWN.',
              'Reasoning tokens are reported but never billed again on top of output. Missing usage remains UNKNOWN.',
              'Rates and their source dates are preserved in usage.csv; current prices still require confirmation.','']
    usage, exported_calls, measured_runs, pilot = [],[],[],None
    seen_calls = set()
    for run_id in run_ids:
        if not run_id or run_id in ('.','..') or any(c in run_id for c in '/\\:'):
            raise ValueError('Invalid run ID')
        folder = ROOT/'runs'/run_id
        latest = json.loads((folder/'run_summary.json').read_text(encoding='utf-8'))
        profile = json.loads((folder/'ingestion_report.json').read_text(encoding='utf-8'))
        source = records(folder,latest['label_config'])
        if len(source)!=100 or len({r['review_id'] for r in source})!=100:
            raise ValueError('Pilot must contain exactly 100 unique source IDs')
        expected = json.loads((ROOT/'data'/'manifest.json').read_text(encoding='utf-8'))['files']['cost_100.csv']['sha256']
        if profile['file_sha256']!=expected:
            raise ValueError('Pilot input does not match the unchanged cost_100.csv checksum')
        if pilot is not None and {r['review_id'] for r in pilot}!={r['review_id'] for r in source}:
            raise ValueError('Cold/warm runs must use the same 100 IDs')
        pilot = source
        all_calls = lines(folder/'calls.jsonl') if (folder/'calls.jsonl').exists() else []
        log = lines(folder/'run_log.jsonl')
        invocations = {}
        for entry in log:
            invocations.setdefault(entry['invocation_id'],[]).append(entry)
        for invocation,stages in invocations.items():
            if any(s['status']=='dry_run' for s in stages):
                continue
            index = len(measured_runs)
            phase = 'cold' if index==0 else 'warm'
            summary_path = folder/f'run_summary_{phase}.json'
            summary = json.loads(summary_path.read_text(encoding='utf-8')) if summary_path.exists() else latest
            matching = summary.get('invocation_id')==invocation
            start,end = min(s['start'] for s in stages),max(s['end'] for s in stages)
            calls = [c for c in all_calls if start <= c['timestamp'] <= end]
            measurements = measure(calls,stages,rates,source,summary)
            measured_runs.append(measurements)
            for call in calls:
                key = (run_id,call['request_id'])
                if key in seen_calls:
                    continue
                seen_calls.add(key)
                exported_calls.append(dict(call,run_id=run_id,invocation_id=invocation,pilot_phase=phase))
                usage.extend(dict(r,run_id=run_id,invocation_id=invocation,pilot_phase=phase) for r in bill(call,rates))
            totals = summary['totals'] if matching else {status:sum(r['status']==status for r in source) for status in ('pending','completed','quarantined')}
            seconds = summary['wall_clock_seconds'] if matching else (datetime.fromisoformat(end)-datetime.fromisoformat(start)).total_seconds()
            cost = total(m['cost'] for role,m in measurements.items() if role in ROLES)
            enriched_ids = {rid for c in calls if c['role']=='enrich' for rid in c['review_ids']}
            cache_hits = sum(bool(r['cache_source_id']) for r in source if r['status']=='completed') if phase=='cold' else sum(r['status']=='completed' and r['review_id'] not in enriched_ids for r in source)
            report += [f'## {run_id} / {phase} / {invocation}','',f'Input SHA-256: `{profile["file_sha256"]}`',
                       '100 IDs: '+', '.join(r['review_id'] for r in source),'',
                       f'Counts: {totals}; distinct nonempty texts: {profile["distinct_nonempty_texts"]}; result-cache hits: {cache_hits}.',
                       f'Total wall-clock seconds: {seconds}'+(' (saved invocation measurement).' if matching else ' (logged stage span).'),
                       f'API subtotal USD: {display(cost)}; per 1,000 rows: {display(None if cost is None else cost*10)}; per completed record: {display(None if cost is None or not totals.get("completed") else cost/totals["completed"])}.',
                       '', '| Stage | Model / effort / prompt / batch / workers | Calls success/fail | Input / cached / output / reasoning tokens | USD | Seconds | Measured work |',
                       '|---|---|---|---|---|---|---|']
            for role in ROLES:
                m = measurements[role]
                report.append(f'| {role} | {m["model"]} / {m["effort"]} / {m["prompt"]} / {m["batch"]} / {m["workers"]} | {m["succeeded"]}/{m["failed"]} | '+
                              ' / '.join(display(m['tokens'][k]) for k in ('input_tokens','cached_input_tokens','output_tokens','reasoning_tokens'))+
                              f' | {display(m["cost"])} | {m["seconds"]} | {m["work"]} |')
            for stage in ('prepare','rank'):
                report.append(f'| {stage} | code only | 0/0 | 0 / 0 / 0 / 0 | 0 | {measurements[stage]["seconds"]} | 100 source rows |')
            if phase=='warm':
                report += ['', 'Warm zero-new-enrichment check: '+('PASS' if measurements['enrich']['calls']==0 else 'FAIL')]
            report.append('')
    if not measured_runs:
        raise ValueError('No executed pilot invocations found; dry-runs are not measurements')
    report += ['## Full-run estimates','',
               '660,622 rows; 660,609 nonempty; 13 quarantined; 484,189 distinct nonempty texts.',
               f'Budget USD: {budget}; modeled enrich workers: {workers}. Verification work: 2000; group and memo costs/time occur once.',
               'Enrich/verify scale by their own successful measured work counts; all measured retry/failure charges are included.',
               'Prepare/rank time scales by rows. Conservative case adds 20% API retries. Local-compute price remains UNKNOWN.',
               'Worker speedup is modeled, not measured, and assumes no rate-limit or resource contention.',
               '', '| Reuse | Case | Estimated API USD | Estimated seconds | Budget |', '|---|---|---|---|---|']
    cold = measured_runs[0]
    for reuse in (True,False):
        for name,multiplier in (('base',Decimal(1)),('conservative',Decimal('1.2'))):
            estimate = estimates(cold,Decimal(100),workers,reuse,multiplier)
            warning = 'UNKNOWN' if estimate['api_cost'] is None else ('WARNING: OVER BUDGET' if estimate['api_cost']>budget else 'within budget')
            report.append(f'| {"with exact reuse" if reuse else "without reuse"} | {name} | {display(estimate["api_cost"])} | {display(estimate["seconds"])} | {warning} |')
    enrich = cold['enrich']
    if enrich['calls'] and enrich['seconds']:
        seconds_per_call = Decimal(str(enrich['seconds']))/enrich['calls']
        report += ['',f'Enrich seconds/call: {seconds_per_call}; modeled seconds/call at {workers} workers: {seconds_per_call/workers}; '+
                   f'modeled distinct-text throughput/second: {Decimal(enrich["work"])/enrich["calls"]*workers/seconds_per_call}.']
    output = ROOT/'cost'
    (output/'report.md').write_text('\n'.join(report)+'\n',encoding='utf-8')
    with (output/'usage.csv').open('w',encoding='utf-8',newline='') as stream:
        writer = csv.DictWriter(stream,fieldnames=USAGE_FIELDS)
        writer.writeheader()
        writer.writerows(usage)
    (output/'pilot_calls.jsonl').write_text(''.join(json.dumps(c,ensure_ascii=False)+'\n' for c in exported_calls),encoding='utf-8')
    exports = []
    for row in pilot:
        result = dict(review_id=row['review_id'],source_sha256=row['source_sha256'],status=row['status'])
        if row['status']=='completed' and row['result_json']:
            result.update(json.loads(row['result_json']),label_config=latest['label_config'])
            if row['cache_source_id']:
                result['cache_source_id'] = row['cache_source_id']
        elif row['status']=='quarantined':
            result['reason'] = row['reason']
        else:
            raise ValueError('Pilot has pending/unclassified records; finish it before exporting final pilot_records.jsonl')
        exports.append(result)
    (output/'pilot_records.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in exports),encoding='utf-8')
    print('Offline replay saved cost/report.md, usage.csv, pilot_records.jsonl and pilot_calls.jsonl.')
    for line in report:
        if 'WARNING' in line:
            print(line)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs',nargs='+',required=True)
    parser.add_argument('--budget-usd',type=Decimal,default=Decimal(40))
    parser.add_argument('--workers',type=int,default=20)
    args = parser.parse_args()
    if args.workers<=0 or not args.budget_usd.is_finite() or args.budget_usd<0:
        parser.error('workers must be positive and budget finite/nonnegative')
    run(args.runs,args.budget_usd,args.workers)


if __name__=='__main__':
    main()
