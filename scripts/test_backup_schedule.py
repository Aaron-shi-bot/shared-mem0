"""Isolated scheduler checks; never import the running server or access live data."""
import ast
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from backup_schedule import seconds_until_backup, stored_backup_status, run_backup_schedule

NOW = 1_800_000_000
DAY = 86400


class StopAfterWait:
    def __init__(self, count=1, on_wait=None):
        self.waits = []
        self.count = count
        self.on_wait = on_wait

    def is_set(self):
        return len(self.waits) >= self.count

    def wait(self, seconds):
        self.waits.append(seconds)
        if self.on_wait:
            self.on_wait(len(self.waits))


class BackupScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Match the current server backup gate; legacy encrypted filenames
        # would make its AST-extracted function check a different fixture.
        self.latest = self.root / 'backups/portable/latest.zip'
        self.latest.parent.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def snapshot(self, modified=NOW, body=b'fixture snapshot'):
        self.latest.write_bytes(body)
        os.utime(self.latest, (modified, modified))

    def remaining(self, clock=NOW):
        return seconds_until_backup(self.latest, DAY, clock)

    def test_missing_empty_and_directory_are_due(self):
        self.assertEqual(self.remaining(), 0)
        self.snapshot(body=b'')
        self.assertEqual(self.remaining(), 0)
        self.latest.unlink()
        self.latest.mkdir()
        self.assertEqual(self.remaining(), 0)

    def test_recent_snapshot_survives_scheduler_restart(self):
        self.snapshot(NOW - 123)
        calls = []
        for _ in range(2):
            stop = StopAfterWait()
            run_backup_schedule(self.latest, DAY, lambda **kw: calls.append(kw), stop,
                                self.fail, clock=lambda: NOW)
        self.assertEqual(calls, [])
        self.assertEqual(self.remaining(), DAY - 123)

    def test_exact_expiry_and_clock_jump_are_due(self):
        self.snapshot(NOW - DAY)
        self.assertEqual(self.remaining(), 0)
        self.assertEqual(self.remaining(NOW + DAY), 0)

    def test_future_mtime_is_replaced_instead_of_delaying_for_days(self):
        self.snapshot(NOW + DAY * 3)
        self.assertEqual(self.remaining(), 0)

    def test_due_backup_only_once_and_then_waits(self):
        self.snapshot(NOW - DAY)
        calls = []
        def create(**kw):
            calls.append(kw)
            self.snapshot()
        stop = StopAfterWait(count=2)
        run_backup_schedule(self.latest, DAY, create, stop, self.fail, clock=lambda: NOW)
        self.assertEqual(calls, [{'force': False}])
        self.assertEqual(stop.waits, [60, 60])

    def test_manual_backup_moves_next_due_time(self):
        self.snapshot(NOW - DAY + 30)
        calls = []
        stop = StopAfterWait(count=2, on_wait=lambda n: self.snapshot() if n == 1 else None)
        run_backup_schedule(self.latest, DAY, lambda **kw: calls.append(kw), stop,
                            self.fail, clock=lambda: NOW)
        self.assertEqual(calls, [])
        self.assertEqual(stop.waits, [30, 60])
        self.assertEqual(self.remaining(), DAY)

    def test_failure_waits_five_minutes(self):
        errors = []
        def fail(**kw):
            raise OSError('fixture unavailable')
        stop = StopAfterWait(count=2)
        run_backup_schedule(self.latest, DAY, fail, stop, errors.append, clock=lambda: NOW)
        self.assertEqual(stop.waits, [300, 300])
        self.assertEqual(len(errors), 2)

    def test_no_snapshot_after_callback_does_not_spin(self):
        stop = StopAfterWait()
        run_backup_schedule(self.latest, DAY, lambda **kw: None, stop, self.fail, clock=lambda: NOW)
        self.assertEqual(stop.waits, [300])

    def test_status_restores_local_and_shared_times(self):
        self.snapshot(NOW - 15)
        shared = self.root / 'shared.enc'
        shared.write_bytes(b'fixture')
        os.utime(shared, (NOW - 30, NOW - 30))
        status = stored_backup_status(self.latest, shared)
        self.assertIsNotNone(status['last_local'])
        self.assertIsNotNone(status['last_shared'])
        self.assertNotEqual(status['last_local'], status['last_shared'])
        self.assertIsNone(status['error'])
        self.assertEqual(self.remaining(), DAY - 15)

    def test_stat_errors_are_reported_without_crashing_initialization(self):
        with patch('backup_schedule.snapshot_mtime', side_effect=PermissionError('fixture denied')):
            self.assertIn('PermissionError', stored_backup_status(self.latest)['error'])

    def server_backup_function(self):
        # Execute the actual small lock/due gate, without loading Mem0/Qdrant.
        source = (Path(__file__).resolve().parents[1] / 'app/server.py').read_text(encoding='utf-8')
        fn = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == 'create_backup')
        calls = []
        def create():
            calls.append(True)
            self.snapshot()
            return {'created': True}
        env = {'backup_lock': threading.Lock(), 'DATA_ROOT': self.root,
               'CONFIG': {'backup': {'interval_seconds': DAY}},
               'backup_status': {'last_local': 'unchanged', 'last_shared': 'unchanged', 'error': 'prior warning'},
               'seconds_until_backup': lambda p, interval: seconds_until_backup(p, interval, NOW),
               '_create_backup': create}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), 'server-backup-gate', 'exec'), env)
        return env, calls

    def test_manual_force_default_and_auto_skip_preserve_status(self):
        self.snapshot()
        env, calls = self.server_backup_function()
        self.assertTrue(env['create_backup'](force=False)['skipped'])
        self.assertEqual(env['backup_status']['error'], 'prior warning')
        self.assertEqual(calls, [])
        self.assertTrue(env['create_backup']()['created'])
        self.assertEqual(len(calls), 1)

    def test_auto_rechecks_due_inside_lock_after_manual_backup(self):
        self.snapshot(NOW - DAY)
        env, calls = self.server_backup_function()
        result = []
        with env['backup_lock']:
            thread = threading.Thread(target=lambda: result.append(env['create_backup'](force=False)))
            thread.start()
            self.snapshot()  # A manual backup completes while automatic work waits.
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(calls, [])
        self.assertTrue(result[0]['skipped'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
