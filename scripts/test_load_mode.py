#!/usr/bin/env python3
"""Transport mocks for the destructive boundaries of the Load Mode experiment."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import load_mode as lm

ORIGINAL = bytes.fromhex('01 01 01 f4 00 1e c8 14 08 00 3c 0e 10 00 0a 46 05 0f 05 0f 03 20 00 32 00 64 46 50 0a 0e e6 0e')
READING = dict(voltage_mV=4300, current_mA=0, temperature_dK=3000, safety_status=0)


class Fake(lm.ModeGauge):
    def __init__(self):
        self.flash = ORIGINAL
        self.pending = self.flash
        self.writes = []
        self.resets = 0
        self.status = 11
        self.fail_commit = False
        self.clear_qen = False
        self.wrong_active = False

    def block(self, cls, index):
        assert (cls, index) == (80, 0)
        self.pending = self.flash
        return self.flash, lm.checksum(self.flash)

    def transfer(self, *args):
        self.writes.append(args)
        assert args[0] == 'w2@0x55'
        reg, val = int(args[1], 0), int(args[2], 0)
        if reg == 0x41:
            self.pending = self.pending[:1] + bytes([val]) + self.pending[2:]
        elif reg == 0x60:
            assert val == lm.checksum(self.pending)
            self.flash = self.pending
            if self.fail_commit:
                self.fail_commit = False
                raise OSError('commit reached hardware but reply failed')
        else:
            raise AssertionError('unexpected configuration write')

    def control_query(self, query):
        return {0: self.status, 1: 0x0542, 2: 0x0201}[query]

    def reset_once(self):
        self.resets += 1
        mode = 1 if self.wrong_active and self.flash[1] == 0 else self.flash[1]
        self.status = (mode << 3) | 2 | (0 if self.clear_qen else 1)


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.backup = Path(self.temp.name) / 'private' / 'backup.json'
        for target, kwargs in [
            ('load_mode.rg.validate', {}),
            ('load_mode.charger_connected', {'return_value': True}),
            ('load_mode.rg.charger_online', {'return_value': True}),
            ('load_mode.rg.sample', {'return_value': READING}),
            ('load_mode.rg.capture', {'return_value': {'80:0': ORIGINAL.hex()}}),
            ('load_mode.time.sleep', {})]:
            mock = patch(target, **kwargs)
            value = mock.start()
            self.addCleanup(mock.stop)
            if target.endswith('rg.sample'):
                self.sample = value
        self.output = io.StringIO()
        ctx = contextlib.redirect_stdout(self.output)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)

    def test_success_writes_only_mode_checksum_and_restores(self):
        g = Fake()
        lm.experiment(g, self.backup)
        self.assertEqual(g.flash, ORIGINAL)
        self.assertEqual(g.resets, 2)
        self.assertEqual([int(w[1], 0) for w in g.writes], [0x41, 0x60, 0x41, 0x60])
        self.assertEqual(lm.read_backup(self.backup), ORIGINAL)
        self.assertEqual(self.backup.stat().st_mode & 0o777, 0o600)
        self.assertIn('experiment_complete', self.output.getvalue())

    def test_existing_backup_aborts_before_write(self):
        self.backup.parent.mkdir()
        self.backup.write_text('keep')
        g = Fake()
        with self.assertRaises(FileExistsError): lm.experiment(g, self.backup)
        self.assertEqual(g.writes, [])
        self.assertEqual(self.backup.read_text(), 'keep')

    def test_fsync_failure_aborts_before_write(self):
        g = Fake()
        with patch.object(lm.os, 'fsync', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError): lm.experiment(g, self.backup)
        self.assertEqual(g.writes, [])

    def test_failed_commit_still_restores(self):
        g = Fake(); g.fail_commit = True
        with self.assertRaises(OSError): lm.experiment(g, self.backup)
        self.assertEqual(g.flash, ORIGINAL)
        self.assertEqual(g.resets, 1)

    def test_signal_exception_during_sampling_restores(self):
        g = Fake()
        self.sample.side_effect = [READING, READING, InterruptedError('SIGTERM'), READING, READING]
        with self.assertRaises(InterruptedError): lm.experiment(g, self.backup)
        self.assertEqual(g.flash, ORIGINAL)

    def test_clear_qen_is_inconclusive_but_mode_restores(self):
        g = Fake(); g.clear_qen = True
        with self.assertRaisesRegex(RuntimeError, 'QEN cleared'): lm.experiment(g, self.backup)
        self.assertEqual(g.flash, ORIGINAL)
        self.assertEqual(g.resets, 2)

    def test_wrong_active_bit_restores(self):
        g = Fake(); g.wrong_active = True
        with self.assertRaisesRegex(RuntimeError, 'LDMD'): lm.experiment(g, self.backup)
        self.assertEqual(g.flash, ORIGINAL)

    def test_recovery_refuses_other_byte_changes(self):
        g = Fake(); g.flash = bytes([2, 0]) + ORIGINAL[2:]
        with self.assertRaisesRegex(RuntimeError, 'other IT Cfg'): lm.restore(g, ORIGINAL)
        self.assertEqual(g.writes, [])

    def test_backup_tampering_refused(self):
        lm.save_backup(self.backup, ORIGINAL, {}, READING)
        data = json.loads(self.backup.read_text()); data['original_block'] = '00' * 32
        self.backup.write_text(json.dumps(data))
        with self.assertRaises(RuntimeError): lm.read_backup(self.backup)

    def test_offline_charger_refused(self):
        g = Fake()
        with patch.object(lm, 'charger_connected', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'offline'): lm.experiment(g, self.backup)
        self.assertEqual(g.writes, [])
        self.assertFalse(self.backup.exists())

    def test_restore_output_broken_pipe_does_not_stop_write(self):
        class DeadOutput:
            def write(self, value): raise BrokenPipeError()
            def flush(self): raise BrokenPipeError()
        g = Fake(); g.flash = ORIGINAL[:1] + b'\x00' + ORIGINAL[2:]
        with contextlib.redirect_stdout(DeadOutput()): lm.restore(g, ORIGINAL)
        self.assertEqual(g.flash, ORIGINAL)

    def test_stale_block_refused_before_write(self):
        g = Fake(); changed = ORIGINAL[:2] + bytes([2]) + ORIGINAL[3:]
        with self.assertRaisesRegex(RuntimeError, 'changed before write'): g.set_mode(changed, 0)
        self.assertEqual(g.writes, [])

    def test_invalid_mode_refused(self):
        g = Fake()
        with self.assertRaises(ValueError): g.set_mode(ORIGINAL, 2)
        self.assertEqual(g.writes, [])


if __name__ == '__main__':
    unittest.main()
