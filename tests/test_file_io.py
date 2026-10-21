import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from src.file_io import append_jsonl, atomic_text


class FileIOTests(unittest.TestCase):
    def setUp(self):
        self.temp_root = Path(__file__).resolve().parents[1] / 'runs' / 'test_tmp'
        self.temp_root.mkdir(parents=True, exist_ok=True)

    def test_50_concurrent_log_writers(self):
        with tempfile.TemporaryDirectory(dir=self.temp_root) as directory:
            path = Path(directory) / 'calls.jsonl'
            barrier = threading.Barrier(50, timeout=15)
            def write(n):
                barrier.wait()
                append_jsonl(path, {'request_id': n})
            with ThreadPoolExecutor(max_workers=50) as workers:
                list(workers.map(write, range(50)))
            lines = path.read_text(encoding='utf-8').splitlines()
            self.assertEqual(len(lines), 50)
            self.assertEqual({json.loads(line)['request_id'] for line in lines}, set(range(50)))

    def test_replace_retries_permission_error(self):
        with tempfile.TemporaryDirectory(dir=self.temp_root) as directory:
            path = Path(directory) / 'summary.json'
            with patch('src.file_io.os.replace', side_effect=[PermissionError(), None]) as replace, \
                    patch('src.file_io.time.sleep') as sleep:
                atomic_text(path, '{}')
            self.assertEqual(replace.call_count, 2)
            sleep.assert_called_once_with(0.2)

    def test_whole_file_write_rejected_in_worker(self):
        with tempfile.TemporaryDirectory(dir=self.temp_root) as directory:
            path = Path(directory) / 'summary.json'
            with ThreadPoolExecutor(max_workers=1) as workers:
                with self.assertRaisesRegex(RuntimeError, 'main thread'):
                    workers.submit(atomic_text, path, '{}').result()
            self.assertFalse(path.exists())

