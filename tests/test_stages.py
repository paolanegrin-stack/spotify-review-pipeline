from argparse import Namespace
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src import config as C
from src import verify, group, rank
from src.enrich import BudgetStop, FatalAPIError, connect, initialize, Ledger
from src.stage_common import cache_key, cached, live_stage, save_cache, send


def record(rid='one', topic='billing', intent='complaint', severity=3):
    return dict(review_id=rid, source_sha256='hash-' + rid, review_text='Synthetic customer text ' + rid,
                labels=dict(topic=topic, intent=intent, severity=severity, sentiment=-1,
                            entities=[], evidence_quote='Synthetic customer text ' + rid, needs_review=False))


class LogicTests(unittest.TestCase):
    def test_comparison_metrics_and_missing_predictions(self):
        records = [record('a'), record('b'), record('missing')]
        predictions = [dict(review_id='a', topic='billing', intent='complaint', severity=3, sentiment=-1, needs_review=False),
                       dict(review_id='b', topic='other', intent='request', severity=1, sentiment=0, needs_review=True)]
        result = verify.compare(records, predictions)
        self.assertEqual(result['agreement'], {field: .5 for field in verify.FIELDS})
        self.assertEqual(result['severity_mean_absolute_error'], 1)
        self.assertEqual(result['missing'], 1)
        self.assertEqual(result['disagreements'][0]['review_id'], 'b')
        self.assertEqual(result['disagreements'][0]['review_text'], records[1]['review_text'])

    def test_no_predictions_agreement_unavailable(self):
        result = verify.compare([record()], [])
        self.assertIsNone(result['agreement']['topic'])
        self.assertIsNone(result['severity_mean_absolute_error'])

    def test_planted_error_detected_without_mutating_business_record(self):
        original = record()
        before = deepcopy(original)
        prediction = {key: original['labels'][key] for key in verify.FIELDS}
        result = verify.planted_error(original, prediction)
        self.assertTrue(result['synthetic'])
        self.assertTrue(result['flagged'])
        self.assertFalse(result['business_tables_modified'])
        self.assertIn('topic', result['differences'])
        self.assertEqual(original, before)

    def test_verifier_blind_request_and_fixed_sample(self):
        records = [record(str(i)) for i in range(40)]
        selected, declaration = verify.select_sample(records, 20)
        again, _ = verify.select_sample(list(reversed(records)), 20)
        self.assertEqual([r['review_id'] for r in selected], [r['review_id'] for r in again])
        self.assertEqual(declaration['seed'], C.VERIFY_SAMPLE_SEED)
        value, local = verify.build_request(selected)
        reviews = json.loads(value['input'])['reviews']
        self.assertTrue(all(set(row) == {'id', 'review_text'} for row in reviews))
        self.assertEqual(set(local), {f'r{i}' for i in range(1,21)})
        for i, row in enumerate(reviews, 1):
            self.assertEqual(row['id'], f'r{i}')
        self.assertTrue(value['text']['format']['strict'])

    def test_verifier_missing_and_foreign_ids_rejected(self):
        _, local = verify.build_request([record()])
        with self.assertRaises(ValueError):
            verify.validate(dict(records=[]), local)
        with self.assertRaises(ValueError):
            verify.validate(dict(records=[dict(id='foreign', **{k:record()['labels'][k] for k in verify.FIELDS})]), local)

    def test_group_foreign_representative_rejected(self):
        sample = [record()]
        payload = dict(issue_id='ISS-BILLING',issue_name='Subscription problems',
                       description='Customers report subscription problems.',representative_review_ids=['foreign'])
        with self.assertRaisesRegex(ValueError, 'Foreign'):
            group.validate(payload, 'ISS-BILLING', sample)
        payload['representative_review_ids'] = ['one']
        self.assertEqual(group.validate(payload, 'ISS-BILLING', sample)['representative_ids'], ['one'])

    def test_group_samples_bounded_and_membership_excludes_noncomplaints(self):
        records = [record(str(i)) for i in range(45)] + [record('p', intent='praise'),record('r', intent='request'),record('u',intent='unclear')]
        records.append(record('cancel', intent='cancellation'))
        evidence = group.evidence(records)
        self.assertEqual(len(evidence), 1)
        issue, members, sample = evidence[0]
        self.assertEqual(len(sample), 30)
        self.assertEqual(len(members), 46)
        self.assertEqual(len(json.loads(group.build_request(issue, sample)['input'])['evidence']), 30)

    def test_decimal_half_up_rounding(self):
        self.assertEqual(rank.mean_severity(129,128), '1.007813')
        self.assertEqual(rank.mean_severity(10,3), '3.333333')
        self.assertEqual(rank.mean_severity(6,2), '3.000000')

    def test_tie_break_order(self):
        records = [record('p',topic='playback'),record('a',topic='access'),record('b',topic='billing',severity=4)]
        rows = rank.rank_rows(records, rank.membership(records))
        self.assertEqual([r['issue_id'] for r in rows], ['ISS-BILLING','ISS-ACCESS','ISS-PLAYBACK'])
        self.assertEqual([r['rank'] for r in rows], [1,2,3])

    def test_invalid_membership_rejected(self):
        records = [record()]
        for pairs in ([], rank.membership(records)*2, [dict(issue_id='ISS-OTHER',review_id='one')]):
            with self.assertRaises(ValueError):
                rank.rank_rows(records, pairs)


class SavedStageTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / 'runs' / 'test_tmp'
        root.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=root)
        self.root = Path(self.tmp.name)
        (self.root / 'prompts').mkdir()
        for name in ('verify_v1', 'group_v1'):
            (self.root / 'prompts' / f'{name}.md').write_text('Synthetic test prompt', encoding='utf-8')
        self.path = self.root / 'runs' / 'test' / 'pipeline.db'
        self.path.parent.mkdir(parents=True)
        self.rows = [record('a'),record('b')]
        with connect(self.path) as db:
            db.execute('''CREATE TABLE records (source_row INTEGER,review_id TEXT,source_sha256 TEXT,
                review_text TEXT,text_sha256 TEXT,status TEXT,attempts INTEGER,reason TEXT)''')
            initialize(db)
            for i, row in enumerate(self.rows):
                db.execute('INSERT INTO records VALUES (?,?,?,?,?,?,?,?)',
                           (i,row['review_id'],row['source_sha256'],row['review_text'],'text-'+row['review_id'],'completed',1,None))
                db.execute('INSERT INTO enrichment_results VALUES (?,?,?,?,?)',
                           (row['review_id'], C.LABEL_CONFIG, 'text-'+row['review_id'],json.dumps(row['labels']),None))

    def tearDown(self):
        self.tmp.cleanup()

    def test_rank_twice_byte_identical(self):
        with patch('src.stage_common.ROOT', self.root):
            rank.execute('test')
            first = (self.path.parent / 'ranking.csv').read_bytes()
            rank.execute('test')
            self.assertEqual((self.path.parent / 'ranking.csv').read_bytes(), first)
        self.assertIn(b'1,ISS-BILLING,2,6,3.000000,6\n', first)

    def test_role_logging_cache_and_enrichment_attempts_unchanged(self):
        value, local = verify.build_request(self.rows)
        payload = dict(records=[dict(id=key, **{k:row['labels'][k] for k in verify.FIELDS}) for key,row in local.items()])
        response = SimpleNamespace(status='completed',id='fake-response',output_text=json.dumps(payload),
                                   usage=SimpleNamespace(input_tokens=100,output_tokens=20,
                                                         input_tokens_details=SimpleNamespace(cached_tokens=0),
                                                         output_tokens_details=SimpleNamespace(reasoning_tokens=0)),
                                   model_dump=lambda **kw: dict(synthetic_test=True))
        client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kw: response))
        with live_stage(self.path, 'verify', '1') as ledger:
            result = send(value, self.rows, ledger, 'initial',lambda p: verify.validate(p, local),client)
            key = cache_key('verify', value, self.rows)
            save_cache(self.path, 'verify', key, result)
            self.assertEqual(cached(self.path,'verify',key), result)
        log = json.loads((self.path.parent / 'calls.jsonl').read_text(encoding='utf-8'))
        self.assertEqual(log['role'], 'verify')
        self.assertEqual(log['label_config'], C.VERIFY_LABEL_CONFIG)
        self.assertEqual(log['input_tokens'], 100)
        with connect(self.path) as db:
            self.assertEqual([r[0] for r in db.execute('SELECT attempts FROM records')], [1,1])

    def test_zero_budget_stops_before_transport(self):
        value, local = verify.build_request(self.rows)
        client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kw: None))
        with live_stage(self.path,'verify','0') as ledger, patch.object(client.responses,'create') as create:
            with self.assertRaises(BudgetStop):
                send(value,self.rows,ledger,'initial',lambda p: verify.validate(p,local),client)
            create.assert_not_called()

    def test_verify_group_mocked_execution_and_warm_replay(self):
        def create(**value):
            user = json.loads(value['input'])
            if value['text']['format']['name'] == 'verify_v1':
                payload = dict(records=[dict(id=r['id'], topic='billing', intent='complaint',
                                             severity=3, sentiment=-1, needs_review=False) for r in user['reviews']])
            else:
                payload = dict(issue_id=user['issue_id'], issue_name='Subscription issues',
                               description='Customers report subscription issues.',
                               representative_review_ids=[user['evidence'][0]['review_id']])
            return SimpleNamespace(status='completed', id='synthetic-response', output_text=json.dumps(payload),
                                   usage=SimpleNamespace(input_tokens=100,output_tokens=20,
                                                         input_tokens_details=SimpleNamespace(cached_tokens=0),
                                                         output_tokens_details=SimpleNamespace(reasoning_tokens=0)),
                                   model_dump=lambda **kw: dict(synthetic_test=True))
        args = Namespace(run_id='test',sample_size=20,planted_error_test=False,dry_run=False,phase='initial',max_spend_usd='1')
        client = SimpleNamespace(responses=SimpleNamespace(create=create))
        with patch('src.stage_common.ROOT',self.root),patch('openai.OpenAI') as factory,patch('dotenv.load_dotenv'),patch('builtins.print'):
            factory.return_value.__enter__.return_value = client
            verify.run(args)
            group.run(args)
            self.assertEqual(factory.call_count,2)
            factory.reset_mock()
            verify.run(args)
            group.run(args)
            factory.assert_not_called()
        log = [json.loads(line) for line in (self.path.parent/'calls.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([r['role'] for r in log],['verify','group'])
        predictions = [json.loads(line) for line in (self.path.parent/'verify'/'verifier_predictions.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(predictions),2)
        report = (self.path.parent/'verify'/'disagreement_report.md').read_text(encoding='utf-8')
        self.assertIn('| topic | 1.000000 |',report)
        self.assertIn('Severity mean absolute error: 0.000000',report)
        issues = json.loads((self.path.parent/'issues.json').read_text(encoding='utf-8'))
        self.assertEqual(issues[0]['member_count'],2)
        self.assertEqual(issues[0]['representative_ids'],['a'])
        with connect(self.path) as db:
            self.assertEqual([r[0] for r in db.execute('SELECT attempts FROM records')],[1,1])

    def test_shared_schema_failure_logged_without_retry(self):
        from openai import BadRequestError
        import httpx2
        value,local = verify.build_request(self.rows)
        error = BadRequestError("Invalid schema: keyword not supported",response=httpx2.Response(
            400,request=httpx2.Request('POST','https://example.invalid')),body={'error':'synthetic schema rejection'})
        client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kw: None))
        with live_stage(self.path,'verify','1') as ledger,patch.object(client.responses,'create',side_effect=error) as create:
            with self.assertRaises(FatalAPIError):
                send(value,self.rows,ledger,'initial',lambda p: verify.validate(p,local),client)
            self.assertEqual(create.call_count,1)
        event = json.loads((self.path.parent/'calls.jsonl').read_text(encoding='utf-8'))
        self.assertEqual(event['outcome'],'failed')
        self.assertEqual(event['error'],str(error))

    def test_verify_and_group_dry_runs_do_not_construct_client_or_write(self):
        args = Namespace(run_id='test',sample_size=20,planted_error_test=False,dry_run=True,
                         phase='initial',max_spend_usd='0')
        before = self.path.read_bytes()
        with patch('src.stage_common.ROOT', self.root),patch('openai.OpenAI') as factory,patch('builtins.print'):
            verify.run(args)
            group.run(args)
            factory.assert_not_called()
        self.assertEqual(self.path.read_bytes(),before)
        self.assertFalse((self.path.parent/'calls.jsonl').exists())
        self.assertFalse((self.path.parent/'verify').exists())

    def test_planted_cli_uses_saved_prediction_only(self):
        folder = self.path.parent / 'verify'
        folder.mkdir()
        prediction = dict(review_id='a', **{k:self.rows[0]['labels'][k] for k in verify.FIELDS})
        (folder/'verifier_predictions.jsonl').write_text(json.dumps(prediction)+'\n',encoding='utf-8')
        args = Namespace(run_id='test',sample_size=20,planted_error_test=True,dry_run=False,phase='initial',max_spend_usd='0')
        before = self.path.read_bytes()
        with patch('src.stage_common.ROOT',self.root),patch('openai.OpenAI') as factory,patch('builtins.print') as output:
            verify.run(args)
            factory.assert_not_called()
        self.assertTrue(any('"flagged": true' in str(c) for c in output.call_args_list))
        self.assertEqual(self.path.read_bytes(),before)


if __name__ == '__main__':
    unittest.main()
