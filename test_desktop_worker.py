"""Desktop pipe, persistence and data ownership tests; isolated temporary data only."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from desktop_runtime import DataLock

ROOT = Path(__file__).parent


class DesktopWorkerTests(unittest.TestCase):
    def run_worker(self, directory, requests=()):
        args = [sys.executable, str(ROOT / 'desktop_worker.py'), '--data', directory]
        process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        raw = b''.join(json.dumps(r).encode() + b'\n' for r in requests)
        out, err = process.communicate(raw, timeout=15)
        self.assertEqual(err, b'')
        return process.returncode, [json.loads(line) for line in out.splitlines()]

    def test_pipe_state_persists_identity_without_public_token(self):
        with tempfile.TemporaryDirectory() as directory:
            request = {'id': 1, 'method': 'GET', 'path': '/api/state', 'body': {}}
            code, rows = self.run_worker(directory, [request])
            self.assertEqual(code, 0)
            self.assertEqual(rows[0], {'ready': True, 'protocol': 1})
            self.assertEqual(rows[1]['id'], 1)
            self.assertNotIn('token', rows[1]['result']['settings'])
            identity = rows[1]['result']['enrollment']
            code, again = self.run_worker(directory, [request])
            self.assertEqual(code, 0)
            self.assertEqual(again[1]['result']['enrollment'], identity)

    def test_data_directory_cannot_be_opened_by_two_engines(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = DataLock(directory)
            try:
                code, rows = self.run_worker(directory)
                self.assertEqual(code, 1)
                self.assertIn('fatal', rows[0])
                self.assertFalse((Path(directory) / 'settings.dpapi').exists())
            finally:
                lock.close()
            self.assertEqual(self.run_worker(directory)[0], 0)

    def test_unknown_route_and_invalid_frame_do_not_execute(self):
        with tempfile.TemporaryDirectory() as directory:
            code, rows = self.run_worker(directory, [{'id': 1, 'method': 'PUT', 'path': '/api/settings', 'body': {'token': 'cannot-change'}}])
            self.assertEqual(code, 0)
            self.assertIn('error', rows[1])
            self.assertNotIn('cannot-change', json.dumps(rows))
            code, rows = self.run_worker(directory, [{'id': 1, 'method': 'POST', 'path': '/admin/config', 'body': {}}])
            self.assertEqual(rows, [{'ready': True, 'protocol': 1}])


if __name__ == '__main__':
    unittest.main()
