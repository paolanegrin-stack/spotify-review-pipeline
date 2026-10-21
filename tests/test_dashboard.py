from contextlib import closing
import json
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from api.index import handler, payload
from src.load_db import RECORD_COLUMNS, record_rows


class DashboardTests(unittest.TestCase):
    def test_loader_preserves_source_text_and_omits_quarantine_labels(self):
        with closing(sqlite3.connect(':memory:')) as db:
            db.row_factory = sqlite3.Row
            db.executescript('''CREATE TABLE records(source_row,review_id,source_sha256,status,reason,
                review_rating,review_timestamp,app_version);
                CREATE TABLE enrichment_results(review_id,label_config,result_json,cache_source_id);''')
            db.executemany('INSERT INTO records VALUES (?,?,?,?,?,?,?,?)',
                           [(1,'one','sha1','completed',None,'05','original timestamp',''),
                            (2,'two','sha2','quarantined','empty_review_text','1','another timestamp','v1')])
            db.execute('INSERT INTO enrichment_results VALUES (?,?,?,?)',
                       ('one','config',json.dumps(dict(topic='playback',intent='complaint',severity=3,
                        sentiment=-1,needs_review=False,evidence_quote='Invented evidence')),None))
            rows = [dict(zip(RECORD_COLUMNS,row)) for row in record_rows(db,'synthetic','config')]
            self.assertEqual(rows[0]['review_rating'],'05')
            self.assertEqual(rows[0]['review_timestamp'],'original timestamp')
            self.assertEqual(rows[0]['app_version'],'')
            self.assertEqual(rows[0]['topic'],'playback')
            self.assertEqual(rows[1]['reason'],'empty_review_text')
            self.assertIsNone(rows[1]['topic'])
            self.assertIsNone(rows[1]['label_config'])

    def test_completed_without_saved_result_rejected(self):
        with closing(sqlite3.connect(':memory:')) as db:
            db.row_factory=sqlite3.Row
            db.executescript("CREATE TABLE records(source_row,review_id,status); INSERT INTO records VALUES(1,'one','completed');"
                             'CREATE TABLE enrichment_results(review_id,label_config,result_json,cache_source_id);')
            with self.assertRaisesRegex(ValueError,'no result'):
                list(record_rows(db,'run','config'))

    def test_issue_identifiers_are_parameters(self):
        cursor=Mock()
        cursor.fetchall.side_effect=[[dict(issue_id='ISS-PLAYBACK')],[]]
        attack="ISS-X'; DROP TABLE records;--"
        result=payload(cursor,'issues','run',attack)
        self.assertEqual(result['reviews'],[])
        for call in cursor.execute.call_args_list:
            self.assertNotIn(attack,call.args[0])
            self.assertEqual(call.args[1],('run',attack))

    def test_api_error_does_not_disclose_credentials(self):
        request=SimpleNamespace(path='/api/overview',respond=Mock())
        with patch('api.index.dispatch',side_effect=RuntimeError('postgresql://private:secret@host/db')):
            handler.do_GET(request)
        self.assertEqual(request.respond.call_args.args[0],503)
        self.assertNotIn('secret',str(request.respond.call_args))
