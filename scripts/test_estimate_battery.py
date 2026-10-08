import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from estimate_battery import Journal, Model, atomic_write
from record_battery import Recorder, Sample


class Log:
    def __init__(self):
        self.lines = []

    def append(self, text):
        self.lines.extend(text.splitlines())


def interval(journal, epoch, out=0, incoming=0, voltage=4_000_000, current=-100_000, status='Discharging', duration=30):
    journal.line(f'# interval-v2 {epoch} {duration} {incoming} {voltage} {current} {status}')
    journal.line(f'{epoch} {duration} {out}')


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.model = Model(10_000_000)
        self.journal = Journal(self.model)
        self.journal.line('# state 1000 Full')

    def test_net_charge_accounts_for_workload(self):
        interval(self.journal, 1030, out=1_000_000)
        self.journal.line('# state 1030 Charging')
        interval(self.journal, 1060, incoming=500_000, current=100_000, status='Charging')
        self.assertEqual(self.model.values(), (10_000_000, 9_450_000))
        # Full AC adapter power is never added: only battery-terminal energy.
        self.assertEqual(self.model.full, 10_000_000)

    def test_interrupted_charge_is_not_a_full_anchor(self):
        interval(self.journal, 1030, out=5_000_000)
        self.journal.line('# state 1030 Charging')
        interval(self.journal, 1060, incoming=2_000_000, current=500_000, status='Charging')
        self.journal.line('# state 1060 Not_charging')
        self.assertEqual(self.model.values(), (10_000_000, 6_800_000))
        self.assertEqual(self.model.capacity_cycles, 0)
        interval(self.journal, 1090, out=1_000_000)
        self.assertEqual(self.model.values()[1], 5_800_000)

    def test_valid_full_low_full_cycle_learns(self):
        interval(self.journal, 1030, out=8_000_000, voltage=3_250_000)
        self.assertEqual(self.model.values()[1], 0)
        self.journal.line('# state 1030 Charging')
        interval(self.journal, 1060, incoming=10_000_000, voltage=4_350_000, current=100_000, status='Charging')
        self.journal.line('# state 1060 Full')
        self.assertEqual(self.model.values(), (9_600_000, 9_600_000))
        self.assertAlmostEqual(self.model.efficiency, 0.88)
        self.assertEqual(self.model.capacity_cycles, 1)
        self.assertEqual(self.model.efficiency_cycles, 1)

    def test_gapped_charge_cycle_cannot_teach_capacity(self):
        interval(self.journal, 1030, out=8_000_000, voltage=3_250_000)
        self.journal.line('# gap 1030 1100 70 sampling')
        self.journal.line('# state 1100 Charging')
        interval(self.journal, 1130, incoming=10_000_000, current=500_000, status='Charging')
        self.journal.line('# state 1130 Full')
        self.assertEqual(self.model.values(), (10_000_000, 10_000_000))
        self.assertEqual(self.model.capacity_cycles, 0)

    def test_inconsistent_charge_cycle_rejected(self):
        interval(self.journal, 1030, out=8_000_000, voltage=3_250_000)
        self.journal.line('# state 1030 Charging')
        interval(self.journal, 1060, incoming=2_000_000, current=500_000, status='Charging')
        self.journal.line('# state 1060 Full')
        self.assertEqual(self.model.values(), (10_000_000, 10_000_000))
        self.assertEqual(self.model.capacity_cycles, 0)
        self.assertEqual(self.model.efficiency_cycles, 0)

    def test_partial_roundtrip_learns_efficiency_not_capacity(self):
        interval(self.journal, 1030, out=2_000_000)
        self.journal.line('# state 1030 Charging')
        interval(self.journal, 1060, incoming=2_500_000, current=500_000, status='Charging')
        self.journal.line('# state 1060 Full')
        self.assertAlmostEqual(self.model.efficiency, 0.88)
        self.assertEqual(self.model.values(), (10_000_000, 10_000_000))
        self.assertEqual(self.model.capacity_cycles, 0)

    def test_legacy_zeros_do_not_invent_charge(self):
        self.journal.line('1030 30 1000000')
        self.journal.line('1060 30 0')
        self.assertEqual(self.model.values()[1], 9_000_000)
        self.assertTrue(self.model.uncertain)
        self.assertEqual(self.model.legacy_out, 1_000_000)

    def test_implicit_gap_invalidates_cycles(self):
        interval(self.journal, 2000, out=1_000_000)
        self.assertTrue(self.model.uncertain)
        self.assertFalse(self.model.full_cycle['valid'])

    def test_low_voltage_sag_early_in_cycle_ignored(self):
        interval(self.journal, 1030, out=100_000, voltage=3_200_000)
        self.assertEqual(self.model.values()[1], 9_900_000)
        self.assertIsNone(self.model.low_cycle)

    def test_duplicate_and_backward_epochs_are_not_deduplicated(self):
        for epoch in (1030, 1030, 500):
            interval(self.journal, epoch, out=100_000)
        self.assertEqual(self.model.values()[1], 9_700_000)

    def test_malformed_or_mismatched_metadata_not_used(self):
        self.journal.line('# interval-v2 1030 30 -1 4000000 100 Charging')
        self.journal.line('1030 30 1000')
        self.journal.line('# interval-v2 1060 30 999999 4000000 100 Charging')
        self.journal.line('1060 20 1000')
        self.journal.line('1061 30 50 # incomplete record')
        self.assertEqual(self.model.values()[1], 9_998_000)
        self.assertEqual(self.journal.invalid_lines, 2)
        self.assertTrue(self.model.uncertain)

    def test_recorder_to_estimator_end_to_end_and_full_flush(self):
        log = Log()
        recorder = Recorder(log, interval_seconds=30)
        recorder.push(Sample(1000, 0, 4_000_000, 0, 'Full'))
        recorder.push(Sample(1001, 1_000_000_000, 4_000_000, -1_000_000, 'Discharging'))
        for second in range(2, 32):
            recorder.push(Sample(1000 + second, second * 1_000_000_000, 4_000_000, -1_000_000, 'Discharging'))
        recorder.push(Sample(1032, 32_000_000_000, 4_000_000, 1_000_000, 'Charging'))
        recorder.push(Sample(1033, 33_000_000_000, 4_000_000, 0, 'Full'))
        # Completion writes its unsaved partial charge BEFORE the Full anchor.
        self.assertEqual(log.lines[-1], '# state 1033 Full')
        fresh = Journal(Model(10_000_000))
        for line in log.lines:
            fresh.line(line)
        self.assertEqual(fresh.invalid_lines, 0)
        self.assertEqual(fresh.model.values(), (10_000_000, 10_000_000))
        self.assertGreater(sum(int(line.split()[4]) for line in log.lines if line.startswith('# interval-v2')), 0)


class FileAndServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'battery.log'
        self.state = self.root / 'state.json'

    def test_checkpoint_resume_and_incomplete_tail(self):
        self.path.write_text('# state 1000 Full\n# interval-v2 1030 30 0 4000000 -1 Discharging\n1030 30 1000000\n1060 30')
        journal = Journal(Model(10_000_000))
        journal.read(self.path)
        journal.save(self.state)
        restored = Journal.restore(self.state, Model(1))
        restored.read(self.path)
        self.assertEqual(restored.model.values(), (10_000_000, 9_000_000))
        with self.path.open('a') as stream:
            stream.write(' 1000000\n')
        restored.read(self.path)
        self.assertEqual(restored.model.values()[1], 8_000_000)
        restored.read(self.path)
        self.assertEqual(restored.model.values()[1], 8_000_000)

    def test_pending_metadata_survives_checkpoint(self):
        self.path.write_text('# state 1000 Full\n# interval-v2 1030 30 2000000 4000000 1 Charging\n')
        journal = Journal(Model(10_000_000))
        journal.read(self.path)
        journal.save(self.state)
        journal = Journal.restore(self.state, Model())
        with self.path.open('a') as stream:
            stream.write('1030 30 3000000\n')
        journal.read(self.path)
        self.assertEqual(journal.model.values()[1], 8_800_000)

    def test_rotation_and_truncation_mark_uncertainty(self):
        self.path.write_text('# state 1000 Full\n1030 30 1000000\n')
        journal = Journal(Model(10_000_000))
        journal.read(self.path)
        self.path.rename(self.root / 'old.log')
        self.path.write_text('# interval-v2 1060 30 0 4000000 -1 Discharging\n1060 30 1000000\n')
        journal.read(self.path)
        self.assertEqual(journal.model.values()[1], 8_000_000)
        self.assertTrue(journal.model.uncertain)
        self.path.write_text('1090 30 1000\n')
        journal.read(self.path)
        self.assertEqual(journal.model.values()[1], 7_999_000)

    def test_atomic_output_replaces_symlink_without_following(self):
        target = self.root / 'target'
        target.write_text('preserved')
        output = self.root / 'bat_full'
        output.symlink_to(target)
        atomic_write(output, '1000\n')
        self.assertEqual(output.read_text(), '1000\n')
        self.assertEqual(target.read_text(), 'preserved')
        self.assertFalse(output.is_symlink())

    def test_changed_voltage_endpoint_invalidates_old_cycle(self):
        journal = Journal(Model(10_000_000))
        journal.line('# state 1000 Full')
        journal.save(self.state)
        restored = Journal.restore(self.state, Model(empty_uV=3_300_000))
        self.assertEqual(restored.model.empty_uV, 3_300_000)
        self.assertFalse(restored.model.full_cycle['valid'])

    def test_live_charger_transition_updates_before_ten_second_timer(self):
        sysfs = self.root / 'sysfs'
        charger = sysfs / 'bq25890-charger-0'
        battery = sysfs / 'bq27542-0'
        charger.mkdir(parents=True)
        battery.mkdir()
        status = charger / 'status'
        status.write_text('Discharging\n')
        (battery / 'voltage_now').write_text('3800000\n')
        self.path.write_text('# state 1000 Full\n# interval-v2 1030 30 0 4000000 -1 Discharging\n1030 30 1000000\n# state 1030 Discharging\n')
        args = [sys.executable, str(Path(__file__).with_name('estimate_battery.py')),
                '--log', str(self.path), '--state', str(self.state), '--sysfs', str(sysfs),
                '--output-dir', str(self.root), '--initial-full-uwh', '10000000']
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            output = self.root / 'bat_cur'
            deadline = time.monotonic() + 4
            while not output.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(output.read_text(), '9000000\n')
            rival = subprocess.run(args, capture_output=True, text=True, timeout=3)
            self.assertNotEqual(rival.returncode, 0)
            status.write_text('Full\n')
            deadline = time.monotonic() + 3
            while output.read_text() != '10000000\n' and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(output.read_text(), '10000000\n')
            process.send_signal(signal.SIGTERM)
            _, error = process.communicate(timeout=3)
            self.assertEqual(process.returncode, 0, error)
            self.assertEqual(json.loads(self.state.read_text())['model']['current'], 10_000_000)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


if __name__ == '__main__':
    unittest.main()
