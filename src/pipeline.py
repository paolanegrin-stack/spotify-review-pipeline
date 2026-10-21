"""One-command saved-state pipeline. Model calls require the explicit --execute flag."""

import argparse
from contextlib import redirect_stdout, closing
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
import uuid

from . import config as C
from . import prepare, enrich, verify, group, rank, memo
from .prepare import ROOT, canonical, file_sha
from .stage_common import atomic_text, completed_records, digest, csv_text

STAGES = ('prepare','enrich','verify','group','rank','memo')
MODELS = ('enrich','verify','group','memo')


def now():
    return datetime.now(timezone.utc).isoformat()


def events(path, after=0):
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='enrichment_calls'").fetchone():
            return []
        return [dict(rowid=row[0], liability_usd=row[1], **json.loads(row[2])) for row in db.execute(
            'SELECT rowid,liability_usd,event_json FROM enrichment_calls WHERE rowid>? ORDER BY rowid',(after,))]


def telemetry(calls):
    fields = ('input_tokens','cached_input_tokens','output_tokens')
    tokens, known, missing = {}, {}, {}
    for field in fields:
        values = [c[field] for c in calls if type(c.get(field)) is int and c[field] >= 0]
        known[field], missing[field] = sum(values), len(calls)-len(values)
        tokens[field] = None if missing[field] else sum(values)
    cost, unknown = Decimal(0), 0
    rates = enrich.read_rates() if calls else None
    for call in calls:
        inp,cached,out = (call.get(k) for k in fields)
        if (call.get('model') != C.MODEL or any(type(v) is not int or v < 0 for v in (inp,cached,out))
                or cached > inp or (cached and rates.get('cached_input') is None)):
            unknown += 1
            continue
        cost += ((inp-cached)*rates['input']+cached*(rates.get('cached_input') or Decimal(0))+out*rates['output'])/Decimal(1000000)
    return dict(calls=dict(total=len(calls),succeeded=sum(c.get('outcome')=='succeeded' for c in calls),
                           failed=sum(c.get('outcome')=='failed' for c in calls)),
                tokens=tokens,known_token_subtotals=known,missing_usage_calls=missing,
                estimated_cost_usd=None if unknown else format(cost,'.8f'),known_cost_subtotal_usd=format(cost,'.8f'),
                unresolved_cost_calls=unknown,
                spent_or_reserved_usd=str(sum((Decimal(c['liability_usd']) for c in calls),Decimal(0))))


def records_digest(path):
    h = hashlib.sha256()
    if not path.is_file():
        return h.hexdigest()
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        for row in db.execute('SELECT review_id,status,attempts,source_sha256 FROM records ORDER BY source_row'):
            h.update((canonical(row)+'\n').encode('utf-8'))
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='enrichment_results'").fetchone():
            for row in db.execute('SELECT review_id,result_json,cache_source_id FROM enrichment_results WHERE label_config=? ORDER BY review_id',(C.LABEL_CONFIG,)):
                h.update((canonical(row)+'\n').encode('utf-8'))
    return h.hexdigest()


def outputs(stage, folder, dry):
    if dry and stage in MODELS:
        names = [f'{stage}_preview.txt']
        if stage == 'memo':
            names.append('claims.csv')
    else:
        names = dict(prepare=['data_manifest.json','ingestion_report.json'],enrich=[],
                     verify=['verify/sample.json','verify/verifier_predictions.jsonl','verify/disagreement_report.md'],
                     group=['membership.csv','issues.json'],rank=['membership.csv','ranking.csv'],
                     memo=['claims.csv','memo.md','memo_check.json'])[stage]
    result = {name:file_sha(folder/name) if (folder/name).is_file() else None for name in names}
    if stage == 'enrich' and not dry:
        result['database_classifications'] = records_digest(folder/'pipeline.db')
    return result


def fingerprint(stage, args, folder, input_sha):
    value = dict(stage=stage,input_sha256=input_sha,execute=args.execute,
                 config={key:getattr(C,key) for key in dir(C) if key.isupper()},
                 code={name:file_sha(ROOT/'src'/f'{name}.py') for name in (stage,'config','stage_common')})
    if stage != 'prepare':
        value['records'] = records_digest(folder/'pipeline.db')
    if stage in MODELS:
        version = C.PROMPT_VERSION if stage == 'enrich' else getattr(C,stage.upper()+'_PROMPT_VERSION')
        value['prompt_sha256'] = file_sha(ROOT/'prompts'/f'{version}.md')
    if stage == 'verify':
        value['sample_size'] = args.verify_sample
    if stage == 'memo':
        value['upstream'] = {name:file_sha(folder/name) if (folder/name).is_file() else None for name in
                             ('ranking.csv','issues.json','verify/verifier_predictions.jsonl','verify/sample.json')}
    return digest(value)


def prepare_or_reuse(args, folder, input_sha):
    if folder.exists():
        manifest_path = folder/'data_manifest.json'
        if not manifest_path.is_file() or not (folder/'pipeline.db').is_file():
            raise ValueError('Incomplete ingestion directory; inspect it and choose a new run ID for re-ingestion')
        saved = json.loads(manifest_path.read_text(encoding='utf-8'))
        if saved['file_sha256'] != input_sha or saved['file_bytes'] != args.input.stat().st_size:
            raise ValueError('Run ID belongs to different input bytes; use a new run ID')
        if saved.get('all_checks_passed') is False:
            raise ValueError('Saved ingestion has failed checks; refusing to proceed')
        with closing(sqlite3.connect((folder/'pipeline.db').resolve().as_uri()+'?mode=ro',uri=True)) as db:
            if db.execute('SELECT COUNT(*) FROM records').fetchone()[0] != saved['total_rows']:
                raise ValueError('Saved ingestion/database row-count mismatch')
        print('prepare: reused saved ingestion; exact input SHA-256 matches.')
        return True
    prepare.prepare(args.input,args.run_id)
    return False


def invoke(stage,args,folder,phase):
    common = dict(run_id=args.run_id,phase=phase,dry_run=not args.execute,max_spend_usd=args.max_spend_usd)
    if stage == 'enrich':
        stage_args = argparse.Namespace(**common,limit=None,workers=args.workers)
        enrich.run(stage_args)
    elif stage == 'verify':
        stage_args = argparse.Namespace(**common,sample_size=args.verify_sample,planted_error_test=False)
        verify.run(stage_args)
    elif stage == 'group':
        stage_args = argparse.Namespace(**common)
        group.run(stage_args)
    elif stage == 'rank':
        # A dry-run of group never writes membership; this code-only adapter can.
        rows = completed_records(folder/'pipeline.db')
        atomic_text(folder/'membership.csv',csv_text(('issue_id','review_id'),rank.membership(rows)))
        rank.execute(args.run_id)
        stage_args = argparse.Namespace()
    elif stage == 'memo':
        stage_args = argparse.Namespace(**common)
        memo.run(stage_args)
    else:
        raise ValueError('Unknown stage')
    if getattr(stage_args,'spend_stopped',False):
        return 'budget_stopped'
    if getattr(stage_args,'stage_failed',False):
        return 'failed_checks'
    if getattr(stage_args,'stage_blocked',False):
        return 'blocked_insufficient_evidence'
    return 'dry_run' if not args.execute and stage in MODELS else 'completed'


def run(args):
    started = time.monotonic()
    if not args.run_id or args.run_id in ('.','..') or any(c in args.run_id for c in '/\\:'):
        raise ValueError('Invalid run ID')
    cap = Decimal(args.max_spend_usd)
    if not cap.is_finite() or cap < 0 or args.verify_sample <= 0 or not 1 <= args.workers <= C.MAX_WORKERS:
        raise ValueError('Invalid spend cap, verification sample or worker count')
    input_sha = file_sha(args.input)
    folder = ROOT/'runs'/args.run_id
    folder.parent.mkdir(parents=True,exist_ok=True)
    lock_path = folder.parent/f'.pipeline-{args.run_id}.lock'
    lock = lock_path.open('x')
    lock.close()
    invocation = uuid.uuid4().hex
    path = folder/'pipeline.db'
    initial_last = 0
    state_path = folder/'pipeline_state.json'
    log_entries, status = [], 'completed' if args.execute else 'dry_run_complete'
    try:
        initial = events(path)
        initial_last = initial[-1]['rowid'] if initial else 0
        phase = 'resume' if initial else 'initial'
        state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
        for stage in STAGES:
            start, clock = now(), time.monotonic()
            prior_calls = events(path)
            last = prior_calls[-1]['rowid'] if prior_calls else 0
            reused, stage_status, error = False, 'completed', None
            try:
                key = fingerprint(stage,args,folder,input_sha)
                previous = state.get(stage,{})
                if stage == 'prepare':
                    reused = prepare_or_reuse(args,folder,input_sha)
                elif (previous.get('fingerprint') == key
                      and previous.get('status') in ('completed','dry_run')
                      and previous.get('outputs') == outputs(stage,folder,not args.execute)):
                    reused = True
                    stage_status = previous['status']
                    print(stage + ': reused unchanged saved outputs; no model call.')
                else:
                    if stage in MODELS and not args.execute:
                        with (folder/f'{stage}_preview.txt').open('w',encoding='utf-8',newline='\n') as preview_file,redirect_stdout(preview_file):
                            stage_status = invoke(stage,args,folder,phase)
                        print(f'{stage}: dry-run preview saved to {folder / (stage+"_preview.txt")}')
                    else:
                        stage_status = invoke(stage,args,folder,phase)
                # Enrichment changes the records digest; cache its resulting state.
                key = fingerprint(stage,args,folder,input_sha)
                state[stage] = dict(fingerprint=key,status=stage_status,outputs=outputs(stage,folder,not args.execute))
                atomic_text(state_path,json.dumps(state,indent=2,sort_keys=True)+'\n')
            except (Exception,SystemExit,KeyboardInterrupt) as exc:
                stage_status = 'interrupted' if isinstance(exc,KeyboardInterrupt) else 'failed'
                error = str(exc) or type(exc).__name__
            entry = dict(invocation_id=invocation,stage=stage,start=start,end=now(),
                         wall_clock_seconds=round(time.monotonic()-clock,6),status=stage_status,reused=reused,
                         **telemetry(events(path,last)))
            if error:
                entry['error'] = error
            folder.mkdir(parents=True,exist_ok=True)
            with (folder/'run_log.jsonl').open('a',encoding='utf-8',newline='\n') as log:
                log.write(canonical(entry)+'\n')
            log_entries.append(entry)
            print(f'{stage}: {"reused" if reused else "recomputed"}; status={stage_status}; calls={entry["calls"]["total"]}')
            if stage_status in ('budget_stopped','failed','failed_checks','blocked_insufficient_evidence','interrupted'):
                status = stage_status
                print('Pipeline stopped: ' + stage_status + (': '+error if error else '') + '. Re-run the same command to resume saved progress.')
                break
    except Exception:
        status = 'failed'
        print('Pipeline metadata/error handling failed; saved stage results remain available for inspection.')
        raise
    finally:
        try:
            db_state = enrich.snapshot(path)
            summary_data = dict(run_id=args.run_id,invocation_id=invocation,status=status,execute=args.execute,
                                input_path=str(args.input.resolve()),input_sha256=input_sha,
                                totals={key:(db_state['statuses'] or {}).get(key,0) for key in ('pending','completed','quarantined')},
                                config={key:getattr(C,key) for key in dir(C) if key.isupper()},
                                label_config=C.LABEL_CONFIG,prompt_versions=dict(enrich=C.PROMPT_VERSION,verify=C.VERIFY_PROMPT_VERSION,
                                group=C.GROUP_PROMPT_VERSION,memo=C.MEMO_PROMPT_VERSION),
                                model_ids=dict(enrich=C.MODEL,verify=C.VERIFY_MODEL,group=C.GROUP_MODEL,memo=C.MEMO_MODEL),
                                spend_limit_usd=str(cap),workers=args.workers,verify_sample=args.verify_sample,
                                wall_clock_seconds=round(time.monotonic()-started,6),
                                this_invocation=telemetry(events(path,initial_last)),lifetime=telemetry(events(path)),stages=log_entries)
            atomic_text(folder/'run_summary.json',json.dumps(summary_data,indent=2,sort_keys=True)+'\n')
            print('Final pipeline summary: '+canonical(dict(run_id=args.run_id,status=status,execute=args.execute,
                  calls=summary_data['this_invocation']['calls'],estimated_cost_usd=summary_data['this_invocation']['estimated_cost_usd'],
                  totals=summary_data['totals'],wall_clock_seconds=summary_data['wall_clock_seconds'])))
        finally:
            lock_path.unlink(missing_ok=True)
    return summary_data


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',required=True,type=Path)
    parser.add_argument('--run-id',required=True)
    parser.add_argument('--verify-sample',type=int,default=C.VERIFY_PILOT_SAMPLE_SIZE)
    parser.add_argument('--max-spend-usd',default=C.DEFAULT_MAX_SPEND_USD)
    parser.add_argument('--workers',type=int,default=C.WORKERS)
    parser.add_argument('--execute',action='store_true')
    result = run(parser.parse_args())
    if result['status'] not in ('completed','dry_run_complete'):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
