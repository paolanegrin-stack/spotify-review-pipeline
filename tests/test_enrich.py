import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from argparse import Namespace
from unittest.mock import patch
from decimal import Decimal
from types import SimpleNamespace

from src import config as C
from src.enrich import (BudgetStop, FatalAPIError, Ledger, build_request, connect, initialize,
                        pending, process_batch, validate_response, run)


def source(text='An invented review praising the queue.', rid='original-1'):
    import hashlib
    return dict(review_id=rid, review_text=text, text_sha256=hashlib.sha256(text.encode()).hexdigest())


def result(id='r1', **overrides):
    return dict(id=id, topic='usability', intent='praise', severity=1, sentiment=1,
                entities=[], needs_review=False, **overrides)


class ValidatorTests(unittest.TestCase):
    def check_error(self, payload, text=None):
        valid, errors = validate_response(payload, {'r1': source(text) if text else source()})
        self.assertFalse(valid)
        self.assertIn('r1', errors)

    def test_missing_id(self):
        self.check_error(dict(records=[]))

    def test_foreign_id(self):
        self.check_error(dict(records=[result(id='foreign')]))

    def test_invalid_topic(self):
        row = result()
        row['topic'] = 'invented_topic'
        self.check_error(dict(records=[row]))

    def test_severity_six(self):
        row = result()
        row['severity'] = 6
        self.check_error(dict(records=[row]))

    def test_quote_not_substring(self):
        row = result(evidence_quote='absent quote')
        self.check_error(dict(records=[row]), 'Long invented text. ' * 30)

    def test_valid_record(self):
        original = source()
        valid, errors = validate_response(dict(records=[result()]), {'r1': original})
        self.assertFalse(errors)
        self.assertEqual(valid['r1']['evidence_quote'], original['review_text'])

    def test_duplicate_id(self):
        self.check_error(dict(records=[result(), result()]))

    def test_bool_severity_and_sentiment(self):
        for field in ('severity', 'sentiment'):
            row = result()
            row[field] = True
            self.check_error(dict(records=[row]))

    def test_short_quote_forbidden(self):
        self.check_error(dict(records=[result(evidence_quote='queue')]))

    def test_valid_long_quote(self):
        text = 'Invented music review. ' * 30
        valid, errors = validate_response(dict(records=[result(evidence_quote='music review.')]), {'r1': source(text)})
        self.assertFalse(errors)
        self.assertEqual(valid['r1']['evidence_quote'], 'music review.')

    def test_hard_max_and_local_ids(self):
        with self.assertRaises(ValueError):
            build_request([source()] * 51)
        request, mapping = build_request([source(), source('Long synthetic review. ' * 30, 'original-2')])
        self.assertEqual(set(mapping), {'r1', 'r2'})
        self.assertNotIn('original-1', request['input'])
        self.assertTrue(request['text']['format']['strict'])
        self.assertEqual(request['reasoning'], {'effort': 'none'})


class StateTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path(__file__).resolve().parents[1] / 'runs' / 'test_tmp'
        temp_root.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=temp_root)
        self.path = Path(self.tmp.name) / 'pipeline.db'
        self.original = source()
        with connect(self.path) as db:
            db.executescript('''CREATE TABLE records (
                source_row INTEGER PRIMARY KEY,review_id TEXT,review_text TEXT,text_sha256 TEXT,
                status TEXT,attempts INTEGER,reason TEXT);''')
            for n in (1, 2):
                db.execute('INSERT INTO records VALUES (?,?,?,?,?,?,?)',
                           (n, f'original-{n}', self.original['review_text'], self.original['text_sha256'], 'pending', 0, None))
            initialize(db)
        self.ledger = Ledger(self.path, Decimal('1'), dict(input=Decimal('.1'), output=Decimal('.5'), cached_input=None))

    def tearDown(self):
        self.tmp.cleanup()

    def response(self, payload):
        return SimpleNamespace(id='response-id', status='completed', output_text=json.dumps(payload),
                               usage=SimpleNamespace(input_tokens=100, output_tokens=50,
                                                     input_tokens_details=SimpleNamespace(cached_tokens=0),
                                                     output_tokens_details=SimpleNamespace(reasoning_tokens=0)),
                               model_dump=lambda **kw: dict(test='synthetic_mock'))

    def test_singleton_validation_retry_and_direct_alias(self):
        client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kw: None))
        with patch.object(client.responses, 'create', side_effect=[self.response(dict(records=[])), self.response(dict(records=[result()]))]) as create:
            process_batch([self.original], self.ledger, 'initial', client)
            self.assertEqual(create.call_count, 2)
            self.assertIn('validation_error', create.call_args.kwargs['input'])
        with connect(self.path) as db:
            self.assertEqual([r[0] for r in db.execute('SELECT status FROM records')], ['completed', 'completed'])
            self.assertEqual([r[0] for r in db.execute('SELECT attempts FROM records')], [2, 2])
            aliases = dict(db.execute('SELECT review_id,cache_source_id FROM enrichment_results'))
            self.assertEqual(aliases, {'original-1': None, 'original-2': 'original-1'})
            self.assertEqual(pending(db, None), [])
        self.assertEqual(len((self.path.parent / 'calls.jsonl').read_text(encoding='utf-8').splitlines()), 2)

    def test_second_invalid_quarantines(self):
        client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kw: self.response(dict(records=[]))))
        process_batch([self.original], self.ledger, 'resume', client)
        with connect(self.path) as db:
            for row in db.execute('SELECT status,attempts,reason FROM records'):
                self.assertEqual(row['status'], 'quarantined')
                self.assertEqual(row['attempts'], 2)
                self.assertTrue(row['reason'].startswith('validation_failed:'))

    def test_budget_blocks_before_call_and_survives_restart(self):
        request, local = build_request([self.original])
        self.ledger.reserve([self.original], request, local, 'initial')
        restarted = Ledger(self.path, Decimal('0.000001'), self.ledger.rates)
        with self.assertRaises(BudgetStop):
            restarted.reserve([self.original], request, local, 'resume')
        with connect(self.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM enrichment_calls').fetchone()[0], 1)

    def test_temporary_errors_bounded_and_logged(self):
        from openai import APIConnectionError
        import httpx2
        client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kw: None))
        error = APIConnectionError(request=httpx2.Request('POST', 'https://example.invalid'))
        with patch.object(client.responses, 'create', side_effect=error) as create, patch('src.enrich.time.sleep'):
            process_batch([self.original], self.ledger, 'initial', client)
            self.assertEqual(create.call_count, 3)
        events = [json.loads(s) for s in (self.path.parent / 'calls.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(events), 3)
        self.assertTrue(all(e['outcome'] == 'failed' and e['input_tokens'] is None for e in events))

    def test_schema_rejection_logs_full_error_and_stops_once(self):
        from openai import BadRequestError
        import httpx2
        body = {'error': {'message': "Invalid schema: keyword 'maxItems' is not permitted.",
                          'param': 'text.format.schema', 'code': 'invalid_json_schema'}}
        error = BadRequestError(body['error']['message'],
                                response=httpx2.Response(400, request=httpx2.Request('POST', 'https://example.invalid')),
                                body=body)
        client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kw: None))
        with patch.object(client.responses, 'create', side_effect=error) as create, patch('src.enrich.time.sleep') as sleep:
            with self.assertRaises(FatalAPIError):
                process_batch([self.original], self.ledger, 'initial', client)
            self.assertEqual(create.call_count, 1)
            sleep.assert_not_called()
        self.assertTrue(self.ledger.stopped.is_set())
        events = [json.loads(s) for s in (self.path.parent / 'calls.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['outcome'], 'failed')
        self.assertEqual(events[0]['error'], str(error))
        self.assertEqual(events[0]['error_body'], body)
        self.assertIsNone(events[0]['input_tokens'])
        with connect(self.path) as db:
            self.assertEqual([r[0] for r in db.execute('SELECT status FROM records')], ['pending', 'pending'])
        request, local = build_request([self.original])
        with self.assertRaises(BudgetStop):
            self.ledger.reserve([self.original], request, local, 'initial')

    def test_real_usage_not_byte_bound_is_logged(self):
        request, local = build_request([self.original])
        call_id, event, reserved = self.ledger.reserve([self.original], request, local, 'initial')
        self.assertIsNone(event['input_tokens'])
        self.ledger.finish(call_id, event, reserved, self.response(dict(records=[result()])), None, .01)
        saved = json.loads((self.path.parent / 'calls.jsonl').read_text(encoding='utf-8'))
        self.assertEqual(saved['input_tokens'], 100)
        self.assertEqual(saved['output_tokens'], 50)

    def test_unknown_cached_rate_keeps_reservation(self):
        request, local = build_request([self.original])
        call_id, event, reserved = self.ledger.reserve([self.original], request, local, 'initial')
        response = self.response(dict(records=[result()]))
        response.usage.input_tokens_details.cached_tokens = 50
        self.ledger.finish(call_id, event, reserved, response, None, .01)
        with connect(self.path) as db:
            row = db.execute('SELECT * FROM enrichment_calls').fetchone()
            self.assertEqual(Decimal(row['liability_usd']), reserved)
            self.assertIsNone(json.loads(row['event_json'])['actual_cost_usd'])

    def test_dry_run_no_client_and_no_database_changes(self):
        import hashlib
        test_root = self.path.parent
        run_dir = test_root / 'runs' / 'example'
        run_dir.mkdir(parents=True)
        target = run_dir / 'pipeline.db'
        target.write_bytes(self.path.read_bytes())
        (test_root / 'prompts').mkdir()
        (test_root / 'prompts' / 'enrich_v1.md').write_text('Synthetic test prompt', encoding='utf-8')
        before = hashlib.sha256(target.read_bytes()).hexdigest()
        args = Namespace(run_id='example', limit=None, workers=1, dry_run=True,
                         max_spend_usd='0', phase='initial')
        with patch('src.enrich.ROOT', test_root), patch('openai.OpenAI') as client, patch('builtins.print'):
            run(args)
            client.assert_not_called()
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), before)
        self.assertFalse((run_dir / 'calls.jsonl').exists())


if __name__ == '__main__':
    unittest.main()
