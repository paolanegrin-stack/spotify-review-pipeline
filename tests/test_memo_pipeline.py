from argparse import Namespace
from contextlib import ExitStack, redirect_stdout, closing
from copy import deepcopy
import csv
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src import config as C
from src import memo, pipeline, prepare, enrich, stage_common

PROJECT = Path(__file__).resolve().parents[1]
RID = '11111111-1111-4111-8111-111111111111'


def pack():
    ranking = [dict(rank='1',issue_id='ISS-ACCESS',complaint_count='2',severity_sum='6',mean_severity='3.000000',priority_score='6'),
               dict(rank='2',issue_id='ISS-PLAYBACK',complaint_count='1',severity_sum='4',mean_severity='4.000000',priority_score='4'),
               dict(rank='3',issue_id='ISS-BILLING',complaint_count='1',severity_sum='2',mean_severity='2.000000',priority_score='2')]
    return dict(ranking=ranking,claims=memo.make_claims(ranking),representative_quotes=[dict(review_id=RID,evidence_quote='Synthetic quote')],
                run_totals=dict(rows=100,completed=4,quarantined=0,unresolved=96,needs_review=0),verifier_agreement=None)


def candidate():
    return dict(memo_markdown=f'Prioritize [ISS-ACCESS]: 2 complaints [C1].\nCompare [ISS-PLAYBACK] and [ISS-BILLING].\nEvidence [{RID}].\n'
                'Historical self-selected reviews do not measure churn. Revenue and plan data are absent; classification can be wrong. Quarantined and unresolved records limit coverage.',
                recommended_issue_id='ISS-ACCESS',alternatives=['ISS-PLAYBACK','ISS-BILLING'],cited_claim_ids=['C1'],cited_review_ids=[RID])


class MemoChecks(unittest.TestCase):
    def test_claim_table_generation_preserves_format_and_order(self):
        claims = memo.make_claims(pack()['ranking'])
        self.assertEqual(len(claims),12)
        self.assertEqual(claims[0],dict(claim_id='C1',issue_id='ISS-ACCESS',metric='complaint_count',value='2'))
        self.assertEqual(claims[2]['value'],'3.000000')
        self.assertEqual(claims[4]['claim_id'],'C5')
        self.assertEqual(claims[4]['issue_id'],'ISS-PLAYBACK')

    def test_correct_numbers_accepted(self):
        self.assertEqual(memo.check(candidate(),pack()),[])

    def test_other_years_and_rank_numbers_rejected_in_v2(self):
        value = candidate()
        value['memo_markdown'] += '\nHistorical snapshot from 2023. Consider rank 1.'
        self.assertTrue(memo.check(value,pack()))

    def test_v2_valid_total_snippets_and_invented_number(self):
        evidence = pack()
        evidence['verifier_agreement'] = dict(agreement=dict(topic=.9))
        totals,snippets = memo.citation_data(evidence)
        self.assertEqual([row['claim_id'] for row in totals[:5]],['T1','T2','T3','T4','T5'])
        value = candidate()
        copied = [s for s in snippets if s['claim_id'].startswith('T')]
        value['memo_markdown'] += '\n' + '; '.join(s['snippet'] for s in copied)
        value['cited_claim_ids'] += [s['claim_id'] for s in copied]
        self.assertEqual(memo.check(value,evidence),[])
        value['memo_markdown'] += '\n999 total rows [T1].'
        self.assertTrue(any('Invented numeric value' in p for p in memo.check(value,evidence)))

    def test_invented_number_rejected_even_with_trailing_period(self):
        for number in ('999.','999 [C1].','.9 [C1].','999,999 [C1].'):
            value = candidate()
            value['memo_markdown'] += '\nInvented impact '+number
            self.assertTrue(any('Invented numeric value' in p for p in memo.check(value,pack())))

    def test_unknown_claim_id_rejected(self):
        value = candidate()
        value['cited_claim_ids'].append('C999')
        value['memo_markdown'] += '\nSee [C999].'
        self.assertTrue(any('Unknown' in p for p in memo.check(value,pack())))

    def test_unknown_review_id_rejected(self):
        value = candidate()
        value['cited_review_ids'] = ['22222222-2222-4222-8222-222222222222']
        value['memo_markdown'] = value['memo_markdown'].replace(RID,value['cited_review_ids'][0])
        self.assertTrue(any('Unknown' in p for p in memo.check(value,pack())))

    def test_unknown_issue_and_missing_alternatives_rejected(self):
        value = candidate()
        value['alternatives'] = ['ISS-FOREIGN']
        value['memo_markdown'] += '\n[ISS-FOREIGN]'
        self.assertTrue(memo.check(value,pack()))

    def test_bare_claim_name_does_not_satisfy_numeric_citation(self):
        value = candidate()
        value['memo_markdown'] = value['memo_markdown'].replace('[C1]','C1')
        value['cited_claim_ids'] = []
        self.assertTrue(any('complete citation snippet' in p for p in memo.check(value,pack())))

    def test_memo_effort_is_low_and_schema_strict(self):
        value = memo.build_request(pack())
        self.assertEqual(value['reasoning'],dict(effort='low'))
        self.assertTrue(value['text']['format']['strict'])
        self.assertEqual(value['text']['format']['name'],'memo_v2')


class PipelineTests(unittest.TestCase):
    def setUp(self):
        temp_root = PROJECT/'runs'/'test_tmp'
        temp_root.mkdir(parents=True,exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=temp_root)
        self.root = Path(self.tmp.name)
        for folder in ('src','data','prompts','cost'):
            (self.root/folder).mkdir()
        for source in (PROJECT/'src').glob('*.py'):
            (self.root/'src'/source.name).write_bytes(source.read_bytes())
        for source in (PROJECT/'prompts').glob('*.md'):
            (self.root/'prompts'/source.name).write_bytes(source.read_bytes())
        (self.root/'data'/'manifest.json').write_bytes((PROJECT/'data'/'manifest.json').read_bytes())
        (self.root/'cost'/'rates.csv').write_bytes((PROJECT/'cost'/'rates.csv').read_bytes())
        self.input = self.root/'data'/'synthetic.csv'
        with self.input.open('w',encoding='utf-8',newline='') as stream:
            writer = csv.DictWriter(stream,fieldnames=prepare.FIELDS)
            writer.writeheader()
            for i in range(2):
                writer.writerow(dict(review_id=f'synthetic-{i}',review_text='Entirely invented review',review_rating='3',
                                     review_likes='0',app_version='',review_timestamp='2023-01-01 00:00:00'))
        self.args = Namespace(input=self.input,run_id='offline_test',verify_sample=20,max_spend_usd='1',workers=1,execute=False)
        self.stack = ExitStack()
        for module in (pipeline,prepare,enrich,stage_common,memo):
            self.stack.enter_context(patch.object(module,'ROOT',self.root))

    def tearDown(self):
        self.stack.close()
        self.tmp.cleanup()

    def test_no_execute_makes_zero_api_calls_and_warm_run_reuses(self):
        with patch('openai.OpenAI',side_effect=AssertionError('No-execute must never construct a client')) as client,redirect_stdout(io.StringIO()):
            first = pipeline.run(self.args)
            second = pipeline.run(self.args)
            client.assert_not_called()
        self.assertEqual(first['status'],'dry_run_complete')
        self.assertEqual(first['this_invocation']['calls']['total'],0)
        self.assertEqual(first['this_invocation']['estimated_cost_usd'],'0.00000000')
        self.assertEqual([r['stage'] for r in first['stages']],list(pipeline.STAGES))
        self.assertTrue(all(r['reused'] for r in second['stages']))
        folder = self.root/'runs'/self.args.run_id
        log = [json.loads(line) for line in (folder/'run_log.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(log),12)
        self.assertTrue(all(row['calls']['total']==0 for row in log))
        self.assertFalse((folder/'calls.jsonl').exists())
        self.assertNotIn('Entirely invented review',(folder/'memo_preview.txt').read_text(encoding='utf-8'))
        with closing(sqlite3.connect(folder/'pipeline.db')) as db:
            self.assertEqual(dict(db.execute('SELECT status,COUNT(*) FROM records GROUP BY status')),{'pending':2})
            self.assertEqual(db.execute('SELECT SUM(attempts) FROM records').fetchone()[0],0)

    def test_clean_budget_stop_does_not_start_later_stages(self):
        self.args.execute = True
        def stopped(args):
            args.spend_stopped = True
        with patch('src.enrich.run',side_effect=stopped),patch('openai.OpenAI') as client,redirect_stdout(io.StringIO()):
            result = pipeline.run(self.args)
            client.assert_not_called()
        self.assertEqual(result['status'],'budget_stopped')
        self.assertEqual([r['stage'] for r in result['stages']],['prepare','enrich'])

    def test_changed_input_cannot_reuse_run_id(self):
        with redirect_stdout(io.StringIO()):
            pipeline.run(self.args)
            self.input.write_text(self.input.read_text(encoding='utf-8')+'\n',encoding='utf-8')
            result = pipeline.run(self.args)
        self.assertEqual(result['status'],'failed')
        self.assertIn('different input bytes',result['stages'][0]['error'])

    def memo_mock_run(self, responses):
        with redirect_stdout(io.StringIO()):
            prepare.prepare(self.input,self.args.run_id)
        evidence = pack()
        evidence.update(issues=[],missing_issue_descriptions=[],input_artifacts={'ranking.csv':'synthetic-hash'})
        def response(payload):
            return SimpleNamespace(status='completed',id='synthetic-response',output_text=json.dumps(payload),
                                   usage=SimpleNamespace(input_tokens=100,output_tokens=20,
                                                         input_tokens_details=SimpleNamespace(cached_tokens=0),
                                                         output_tokens_details=SimpleNamespace(reasoning_tokens=0)),
                                   model_dump=lambda **kw:dict(synthetic_test=True))
        client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kw: None))
        args = Namespace(run_id=self.args.run_id,dry_run=False,phase='initial',max_spend_usd='1')
        with patch('src.memo.build_pack',return_value=evidence),patch('dotenv.load_dotenv'),patch('openai.OpenAI') as factory,patch.object(client.responses,'create',side_effect=[response(p) for p in responses]) as create,redirect_stdout(io.StringIO()):
            factory.return_value.__enter__.return_value = client
            memo.run(args)
            self.assertEqual(create.call_count,len(responses))
            self.assertIn('validation_error',json.loads(create.call_args.kwargs['input']))
            self.assertEqual(json.loads(create.call_args.kwargs['input'])['validation_error'],memo.check(responses[-2],evidence))
        return args,evidence

    def test_memo_validation_retry_and_warm_reuse_log_low_effort(self):
        bad = candidate()
        bad['memo_markdown'] += '\n999 invented.'
        args,evidence = self.memo_mock_run([bad,candidate()])
        folder = self.root/'runs'/self.args.run_id
        saved = json.loads((folder/'memo_check.json').read_text(encoding='utf-8'))
        self.assertTrue(saved['passed'])
        calls = [json.loads(line) for line in (folder/'calls.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([c['outcome'] for c in calls],['failed','succeeded'])
        self.assertTrue(all(c['role']=='memo' and c['effort']=='low' for c in calls))
        self.assertEqual(calls[0]['input_artifacts'],evidence['input_artifacts'])
        with patch('src.memo.build_pack',return_value=evidence),patch('openai.OpenAI') as client,redirect_stdout(io.StringIO()):
            memo.run(args)
            client.assert_not_called()

    def test_memo_failed_checks_saved_after_third_invalid_output(self):
        bad = candidate()
        bad['memo_markdown'] += '\n999 invented.'
        args,_ = self.memo_mock_run([bad,bad,bad])
        folder = self.root/'runs'/self.args.run_id
        self.assertTrue(args.stage_failed)
        self.assertFalse(json.loads((folder/'memo_check.json').read_text(encoding='utf-8'))['passed'])
        self.assertTrue((folder/'memo.md').read_text(encoding='utf-8').startswith('# FAILED CHECKS'))


if __name__ == '__main__':
    unittest.main()
