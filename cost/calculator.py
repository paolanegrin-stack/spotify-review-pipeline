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
        known_calls = [c for c in attempted if all(r['cost_usd'] is not None for r in bill(c,rates))]
        unknown_ids = [c['request_id'] for c in attempted if c not in known_calls]
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
        completed = sum(s['records_completed'] for s in stages if s['stage']=='enrich' and 'records_completed' in s)
        if role == 'enrich' and not any('records_completed' in s for s in stages if s['stage']=='enrich'):
            # Legacy logs: infer completed aliases from exact-text matches to successful calls.
            texts = {r['text_sha256'] for r in source if r['review_id'] in ids and r['status']=='completed'}
            completed = sum(r['status']=='completed' and r['text_sha256'] in texts for r in source)
        prefix = '' if role=='enrich' else role.upper()+'_'
        result[role] = dict(calls=len(attempted),succeeded=sum(c['outcome']=='succeeded' for c in attempted),
                            completed=completed if role=='enrich' else 0,
                            failed=sum(c['outcome']=='failed' for c in attempted),work=work,
                            cost=sum((r['cost_usd'] for r in charges if r['cost_usd'] is not None),Decimal(0)),
                            known_calls=len(known_calls),unknown_usage=len(unknown_ids),unknown_request_ids=unknown_ids,
                            seconds=sum(s['wall_clock_seconds'] for s in stages if s['stage']==role),
                            tokens={k:sum((c[k] for c in attempted if type(c.get(k)) is int),0) for k in
                                    ('input_tokens','cached_input_tokens','output_tokens','reasoning_tokens')},
                            model=summary.get('model_ids',{}).get(role,config.get(prefix+'MODEL')),
                            effort=config.get(prefix+'EFFORT'),prompt=summary.get('prompt_versions',{}).get(role),
                            batch=max((len(c['review_ids']) for c in attempted),default=config.get(prefix+'BATCH_SIZE','one aggregate pack')),
                            workers=next((s['workers'] for s in stages if s['stage']=='enrich' and 'workers' in s),
                                         summary.get('workers',1)) if role=='enrich' else 1)
    for stage in ('prepare','rank'):
        result[stage] = dict(seconds=sum(s['wall_clock_seconds'] for s in stages if s['stage']==stage))
    return result


def estimates(measured, rows, workers, reuse=True, multiplier=Decimal('1')):
    target = dict(enrich=484189 if reuse else 660609,verify=2000,group=1,memo=1)
    costs,times = [],[]
    for role in ROLES:
        m = measured[role]
        factor = (Decimal(target[role])/Decimal(m['work']) if m['work'] else None) if role in ('enrich','verify') else (Decimal(1) if m['work'] else None)
        # Use the observed known per-call average for every projected attempt.
        priced_attempt_cost = m['cost']/m['known_calls']*m['calls'] if m['known_calls'] else None
        costs.append(priced_attempt_cost*factor*multiplier if factor is not None and priced_attempt_cost is not None else None)
        seconds = Decimal(str(m['seconds']))*factor*multiplier if factor is not None else None
        if seconds is not None and role=='enrich':
            seconds *= Decimal(m['workers']) / workers
        times.append(seconds)
    local = sum((Decimal(str(measured[stage]['seconds']))*Decimal(target_rows)/rows
                 for stage,target_rows in (('prepare',660622),('rank',660609))),Decimal(0))
    duration = total(times)
    return dict(api_cost=total(costs),seconds=None if duration is None else duration+local)


def display(value):
    return 'UNKNOWN' if value is None else str(value)


def scope_seconds(measured, count, workers):
    """Elapsed stage time already includes concurrency; scale workers only once."""
    completed = sum(m['enrich']['completed'] for m in measured)
    adjusted = sum((Decimal(str(m['enrich']['seconds'])) * Decimal(m['enrich']['workers']) / workers
                    for m in measured), Decimal(0))
    return adjusted * count / completed if completed else None


def run(run_ids, budget, workers):
    rates = load_rates(ROOT/'cost'/'rates.csv')
    report = ['# Pilot cost and runtime replay','',
              'Offline only. USD API costs use saved usage and editable rates.csv; infrastructure/local-compute cost is UNKNOWN.',
              'Reasoning tokens are reported but never billed again on top of output. Token/cost totals are known subtotals; calls without usage are listed separately.',
              'Rates and their source dates are preserved in usage.csv; current prices still require confirmation.','']
    usage, exported_calls, measured_runs, pilot = [],[],[],None
    timing_runs = []
    seen_calls = set()
    for run_id in run_ids:
        if not run_id or run_id in ('.','..') or any(c in run_id for c in '/\\:'):
            raise ValueError('Invalid run ID')
        folder = ROOT/'runs'/run_id
        latest = json.loads((folder/'run_summary.json').read_text(encoding='utf-8'))
        profile = json.loads((folder/'ingestion_report.json').read_text(encoding='utf-8'))
        source = records(folder,latest['label_config'])
        source_name = {100:'cost_100.csv',500:'checkpoint_500.csv',10000:'analysis_10000.csv'}.get(len(source))
        if source_name is None or len({r['review_id'] for r in source})!=len(source):
            raise ValueError('Input must contain a 100, 500 or 10,000-review course checkpoint with unique source IDs')
        expected = json.loads((ROOT/'data'/'manifest.json').read_text(encoding='utf-8'))['files'][source_name]['sha256']
        if profile['file_sha256']!=expected:
            raise ValueError('Input does not match the unchanged '+source_name+' checksum')
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
            if matching and measurements['enrich']['completed'] and any(
                    s['stage']=='enrich' and s['status']=='completed' for s in stages):
                timing_runs.append(measurements)
            for call in calls:
                key = (run_id,call['request_id'])
                if key in seen_calls:
                    continue
                seen_calls.add(key)
                exported_calls.append(dict(call,run_id=run_id,invocation_id=invocation,pilot_phase=phase))
                usage.extend(dict(r,run_id=run_id,invocation_id=invocation,pilot_phase=phase) for r in bill(call,rates))
            totals = summary['totals'] if matching else {status:sum(r['status']==status for r in source) for status in ('pending','completed','quarantined')}
            seconds = summary['wall_clock_seconds'] if matching else (datetime.fromisoformat(end)-datetime.fromisoformat(start)).total_seconds()
            cost = sum((m['cost'] for role,m in measurements.items() if role in ROLES),Decimal(0))
            unknown = sum(measurements[role]['unknown_usage'] for role in ROLES)
            enriched_ids = {rid for c in calls if c['role']=='enrich' for rid in c['review_ids']}
            cache_hits = sum(bool(r['cache_source_id']) for r in source if r['status']=='completed') if phase=='cold' else sum(r['status']=='completed' and r['review_id'] not in enriched_ids for r in source)
            report += [f'## {run_id} / {phase} / {invocation}','',f'Input SHA-256: `{profile["file_sha256"]}`',
                       f'{len(source)} IDs: '+', '.join(r['review_id'] for r in source),'',
                       f'Counts: {totals}; distinct nonempty texts: {profile["distinct_nonempty_texts"]}; result-cache hits: {cache_hits}.',
                       f'Total wall-clock seconds: {seconds}'+(' (saved invocation measurement).' if matching else ' (logged stage span).'),
                       f'API known subtotal USD: {cost} + {unknown} calls with unknown usage; per 1,000 rows (known): {cost*1000/len(source)}; per completed record (known): {display(None if not totals.get("completed") else cost/totals["completed"])}.',
                       '', '| Stage | Model / effort / prompt / batch / workers | Calls success/fail | Input / cached / output / reasoning tokens | USD | Seconds | Measured work |',
                       '|---|---|---|---|---|---|---|']
            unknown_lines = []
            for role in ROLES:
                m = measurements[role]
                report.append(f'| {role} | {m["model"]} / {m["effort"]} / {m["prompt"]} / {m["batch"]} / {m["workers"]} | {m["succeeded"]}/{m["failed"]} | '+
                              ' / '.join(display(m['tokens'][k]) for k in ('input_tokens','cached_input_tokens','output_tokens','reasoning_tokens'))+
                              f' | {m["cost"]} + {m["unknown_usage"]} calls with unknown usage | {m["seconds"]} | {m["work"]} |')
                if m['unknown_usage']:
                    unknown_lines.append(f'Unknown usage ({m["unknown_usage"]}), {role}: '+', '.join(m['unknown_request_ids']))
            for stage in ('prepare','rank'):
                report.append(f'| {stage} | code only | 0/0 | 0 / 0 / 0 / 0 | 0 | {measurements[stage]["seconds"]} | {len(source)} source rows |')
            report += ['',*unknown_lines]
            if phase=='warm':
                report += ['', 'Warm zero-new-enrichment check: '+('PASS' if measurements['enrich']['calls']==0 else 'FAIL')]
            report.append('')
    if not measured_runs:
        raise ValueError('No executed pilot invocations found; dry-runs are not measurements')
    report += ['## Full-run estimates','',
               '660,622 rows; 660,609 nonempty; 13 quarantined; 484,189 distinct nonempty texts.',
               f'Budget USD: {budget}; modeled enrich workers: {workers}. Verification work: 2000; group and memo costs/time occur once.',
               'Enrich/verify scale by their own successful work counts. Projected attempts use known per-call average cost, including retries; unknown charges are estimated, not claimed as measured.',
               'Prepare/rank time scales by rows. Conservative case adds 20% API retries. Local-compute price remains UNKNOWN.',
               'Worker speedup is modeled, not measured, and assumes no rate-limit or resource contention.',
               '', '| Reuse | Case | Estimated API USD | Estimated seconds | Budget |', '|---|---|---|---|---|']
    cold = measured_runs[0]
    for reuse in (True,False):
        for name,multiplier in (('base',Decimal(1)),('conservative',Decimal('1.2'))):
            estimate = estimates(cold,Decimal(len(pilot)),workers,reuse,multiplier)
            warning = 'UNKNOWN' if estimate['api_cost'] is None else ('WARNING: OVER BUDGET' if estimate['api_cost']>budget else 'within budget')
            report.append(f'| {"with exact reuse" if reuse else "without reuse"} | {name} | {display(estimate["api_cost"])} | {display(estimate["seconds"])} | {warning} |')
    enrich = cold['enrich']
    if enrich['calls'] and enrich['seconds']:
        seconds_per_call = Decimal(str(enrich['seconds']))/enrich['calls']
        report += ['',f'Enrich elapsed stage seconds/call (not summed call latency): {seconds_per_call}.']
    estimate_100k = scope_seconds(timing_runs[-1:], 100000, workers)
    report += ['',f'100,000-review enrichment at {workers} workers: base seconds {display(estimate_100k)}; '
               f'conservative (+20%) seconds {display(None if estimate_100k is None else estimate_100k*Decimal("1.2"))}.',
               'Based on completed records / measured enrich-stage wall time in the latest completed enrichment invocation '
               'with matching saved worker settings; worker scaling applied once. '
               'Legacy logs lack completion deltas: completed counts include exact-text aliases inferred from successful calls. '
               'This is enrichment time only, excluding downstream stages; linear scaling assumes no additional contention.']
    output = ROOT/'cost'
    prefix = '' if len(pilot)==100 else run_ids[0]+'_'
    (output/(prefix+'report.md')).write_text('\n'.join(report)+'\n',encoding='utf-8')
    with (output/(prefix+'usage.csv')).open('w',encoding='utf-8',newline='') as stream:
        writer = csv.DictWriter(stream,fieldnames=USAGE_FIELDS)
        writer.writeheader()
        writer.writerows(usage)
    (output/(prefix+'pilot_calls.jsonl')).write_text(''.join(json.dumps(c,ensure_ascii=False)+'\n' for c in exported_calls),encoding='utf-8')
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
    (output/(prefix+'pilot_records.jsonl')).write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in exports),encoding='utf-8')
    print('Offline replay saved '+str(output/(prefix+'report.md'))+' and matching usage/record/call exports.')
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
