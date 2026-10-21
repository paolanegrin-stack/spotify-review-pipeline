import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from src.enrich import connect, initialize, pending, save_batch
from src.scope import install, quarantine, select_ids, snapshot


class ScopeTests(unittest.TestCase):
    def test_seed_order_nonempty_and_determinism(self):
        rows = [dict(review_id=str(n), review_text='text' if n else '  ') for n in range(10)]
        expected = sorted(rows[1:], key=lambda r: hashlib.sha256(('seed:'+r['review_id']).encode()).hexdigest())
        self.assertEqual(select_ids(rows, 'seed', 5), [r['review_id'] for r in expected[:5]])
        self.assertEqual(select_ids(rows, 'seed', 5), select_ids(list(reversed(rows)), 'seed', 5))

    def test_scope_limits_dispatch_aliases_and_explicit_quarantine(self):
        root = Path(__file__).resolve().parents[1]/'runs/test_tmp'
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as folder:
            path = Path(folder)/'pipeline.db'
            with connect(path) as db:
                db.execute('CREATE TABLE records (source_row INTEGER PRIMARY KEY,review_id TEXT,review_text TEXT,'
                           'text_sha256 TEXT,status TEXT,attempts INTEGER,reason TEXT)')
                db.executemany('INSERT INTO records VALUES (?,?,?,?,?,?,?)',
                               [(1,'outside','same','hash','pending',0,None),
                                (2,'inside','same','hash','pending',0,None)])
                initialize(db)
            scope = path.parent/'scope_ids.txt'
            scope.write_text('inside\n', encoding='utf-8')
            install(path, scope)
            with connect(path) as db:
                rows = pending(db, None)
            self.assertEqual([r['review_id'] for r in rows], ['inside'])
            with self.assertRaisesRegex(ValueError, 'Finish'):
                quarantine(path)
            save_batch(path, [(rows[0], {'topic':'other'}, None)])
            with connect(path) as db:
                self.assertEqual(pending(db, None), [])
                self.assertEqual(db.execute("SELECT status FROM records WHERE review_id='outside'").fetchone()[0], 'pending')
            self.assertEqual(snapshot(path, 'checkpoint_after.json'), (1, 0))
            quarantine(path)
            with connect(path) as db:
                self.assertEqual(db.execute("SELECT reason FROM records WHERE review_id='outside'").fetchone()[0],
                                 'not_classified_out_of_declared_scope')
                self.assertEqual(db.execute('SELECT COUNT(*) FROM enrichment_results').fetchone()[0], 1)
            self.assertEqual(json.loads((path.parent/'checkpoint_after.json').read_text())['count'], 1)
