#!/usr/bin/env python3
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import subprocess

import reset_gauge as m


class FakeGauge(m.ResetGauge):
    def __init__(self, sealed=False, wrong=False, fail=False, qen=True):
        self.sealed, self.wrong, self.fail = sealed, wrong, fail
        self.qen = qen
        self.calls = []
        self.did_reset = False
        self.cls = 48
        self.index = 0
        self.query = 0
        self.backup_directory = None

    def transfer(self, *args):
        self.calls.append(args)
        if args[0] == 'w3@0x55':
            self.query = int(args[2], 16) | int(args[3], 16) << 8
            if self.query == 0x41:
                assert self.backup_directory is not None
                assert (Path(self.backup_directory) / 'before.json').is_file()
                self.did_reset = True
                if self.fail:
                    raise subprocess.TimeoutExpired('i2ctransfer', 5)
            else:
                assert self.query in (0, 1, 2, 5)
            return ''
        if args[0] == 'w2@0x55':
            reg, value = int(args[1], 16), int(args[2], 16)
            assert reg in (0x61, 0x3e, 0x3f)
            if reg == 0x3e:
                self.cls = value
            if reg == 0x3f:
                self.index = value
            return ''
        reg, length = int(args[1], 16), int(args[2][1:])
        block = bytes([self.cls + self.index] * 32)
        if reg == 0x40:
            result = block
        elif reg == 0x60:
            result = bytes([255 - (sum(block) & 255)])
        else:
            values = {0x06: 3040, 0x08: 4260, 0x14: 0}
            if reg == 0:
                value = {0: 0x200b if self.sealed else (0x000d if self.did_reset
                            else 0x000b if self.qen else 0x000a),
                         1: 0x9999 if self.wrong else 0x0542,
                         2: 0x0201, 5: 4 + self.did_reset}[self.query]
            else:
                value = values.get(reg, 0)
            result = value.to_bytes(2, 'little')
        assert len(result) == length
        return ' '.join(f'0x{x:02x}' for x in result)


class Tests(unittest.TestCase):
    def run_experiment(self, gauge, directory, online=True, bad_save=False):
        # Exercise the actual capture, backup, reset and compare paths.
        gauge.backup_directory = directory
        from test_dataflash import Client
        with patch.object(m, 'Path', side_effect=lambda p: Client() if str(p).startswith('/sys/bus/i2c/') else Path(p)), \
                patch.object(m, 'charger_online', return_value=online), \
                patch.object(m.tempfile, 'mkdtemp', return_value=directory), \
                patch.object(m.time, 'sleep'), \
                contextlib.redirect_stdout(io.StringIO()):
            if bad_save:
                with patch.object(m, 'save', side_effect=OSError('disk full')):
                    m.experiment(gauge)
            else:
                m.experiment(gauge)

    def test_success_backups_and_single_reset(self):
        g = FakeGauge()
        with tempfile.TemporaryDirectory() as directory:
            self.run_experiment(g, directory)
            self.assertTrue((Path(directory) / 'before.json').is_file())
            self.assertTrue((Path(directory) / 'after.json').is_file())
        resets = [a for a in g.calls if a == ('w3@0x55', '0x00', '0x41', '0x00')]
        self.assertEqual(len(resets), 1)
        # Fake transport rejects all configuration/BlockData/checksum writes.

    def test_refusals_never_reset(self):
        for kwargs, online, bad_save in [({'sealed': True}, True, False),
                                        ({'wrong': True}, True, False),
                                        ({'qen': False}, True, False),
                                        ({}, False, False), ({}, True, True)]:
            with self.subTest(kwargs=kwargs, online=online, bad_save=bad_save):
                g = FakeGauge(**kwargs)
                with tempfile.TemporaryDirectory() as directory, \
                        self.assertRaises((RuntimeError, OSError)):
                    self.run_experiment(g, directory, online, bad_save)
                self.assertFalse(g.did_reset)

    def test_ambiguous_reset_failure_not_retried(self):
        g = FakeGauge(fail=True)
        with tempfile.TemporaryDirectory() as directory, \
                self.assertRaises(subprocess.TimeoutExpired):
            self.run_experiment(g, directory)

        self.assertEqual(sum(a == ('w3@0x55', '0x00', '0x41', '0x00')
                             for a in g.calls), 1)

    def test_vok_clear_is_not_qen_clear(self):
        from test_dataflash import Client
        g = FakeGauge()
        g.did_reset = True  # status 0x000d: QEN=1, VOK=0, RUP_DIS=1
        with patch.object(m, 'Path', return_value=Client()), \
                patch.object(m.time, 'sleep'), \
                contextlib.redirect_stdout(io.StringIO()):
            m.validate(g)

    def test_after_reset_qen_clear_does_not_block_evidence(self):
        from test_dataflash import Client
        g = FakeGauge(qen=False)  # QEN=0, VOK=1
        with patch.object(m, 'Path', return_value=Client()), \
                patch.object(m.time, 'sleep'), \
                contextlib.redirect_stdout(io.StringIO()):
            m.validate(g, require_qen=False)

    def test_power_guards(self):
        base = dict(voltage_mV=4260, current_mA=0, temperature_dK=3040,
                    safety_status=0)
        with patch.object(m, 'charger_online', return_value=True):
            for changed in ({'voltage_mV': 3900}, {'current_mA': -200},
                            {'temperature_dK': 3300}, {'safety_status': 1}):
                with self.assertRaises(RuntimeError):
                    m.power_check(base | changed)


if __name__ == '__main__':
    unittest.main()
