import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from record_battery import AppendLog, DENOMINATOR, Recorder, Sample, read_sample


class MemoryLog:
    def __init__(self):
        self.lines = []

    def append(self, line):
        self.lines.append(line)

    def rows(self):
        return [list(map(int, line.split())) for line in self.lines if not line.startswith('#')]


def sample(second, current=-1_000_000, epoch=None):
    return Sample(1000 + second if epoch is None else epoch,
                  second * 1_000_000_000, 4_000_000, current)


class IntegrationTests(unittest.TestCase):
    def test_units_and_fractional_carry(self):
        log = MemoryLog()
        recorder = Recorder(log)
        for second in range(91):
            recorder.push(sample(second))
        self.assertEqual(log.rows(), [[1030, 30, 33333], [1060, 30, 33333], [1090, 30, 33334]])
        self.assertEqual(sum(row[2] for row in log.rows()), 100000)

    def test_configurable_intervals_preserve_format_and_total(self):
        for interval in (10, 30, 60):
            with self.subTest(interval=interval):
                log = MemoryLog()
                recorder = Recorder(log, interval)
                for second in range(61):
                    recorder.push(sample(second))
                recorder.finish()
                self.assertEqual(sum(row[1] for row in log.rows()), 60)
                self.assertEqual(sum(row[2] for row in log.rows()), 66666)
                self.assertTrue(all(len(row) == 3 for row in log.rows()))

    def test_charging_records_zero(self):
        log = MemoryLog()
        recorder = Recorder(log)
        for second in range(31):
            recorder.push(sample(second, 1_000_000))
        self.assertEqual(log.rows(), [[1030, 30, 0]])

    def test_current_zero_crossings(self):
        for start, end in ((-1_000_000, 1_000_000), (1_000_000, -1_000_000)):
            with self.subTest(start=start):
                log = MemoryLog()
                recorder = Recorder(log, 1)
                recorder.push(sample(0, start))
                recorder.push(sample(1, end))
                # Discharge lasts half the second with a triangular power area.
                self.assertEqual(log.rows(), [[1001, 1, 277]])
                self.assertEqual(recorder.energy_numerator,
                                 1_000_000_000_000_000_000_000 - 277 * DENOMINATOR)

    def test_wall_clock_changes_do_not_change_duration(self):
        log = MemoryLog()
        recorder = Recorder(log, 1)
        recorder.push(sample(0, epoch=1000))
        recorder.push(sample(1, epoch=500))
        recorder.push(sample(2, epoch=500))
        self.assertEqual(log.rows(), [[500, 1, 1111], [500, 1, 1111]])

    def test_unavailable_samples_do_not_bridge_gap(self):
        log = MemoryLog()
        recorder = Recorder(log)
        recorder.push(sample(0))
        recorder.push(sample(2))
        recorder.missing()
        recorder.missing()
        recorder.push(sample(100))
        recorder.push(sample(102))
        recorder.finish()
        self.assertEqual(log.rows(), [[1002, 2, 2222], [1102, 2, 2222]])
        self.assertIn('# gap 1002 1100 98.000 unavailable', log.lines)

    def test_suspend_gap_is_excluded(self):
        log = MemoryLog()
        recorder = Recorder(log)
        for second in (0, 2, 100, 102):
            recorder.push(sample(second))
        recorder.finish()
        self.assertEqual(sum(row[1] for row in log.rows()), 4)
        self.assertEqual(sum(row[2] for row in log.rows()), 4444)
        self.assertIn('# gap 1002 1100 98.000 sampling', log.lines)

    def test_final_partial_interval_saved_once(self):
        log = MemoryLog()
        recorder = Recorder(log)
        recorder.push(sample(0))
        recorder.push(sample(3))
        recorder.finish()
        recorder.finish()
        self.assertEqual(log.rows(), [[1003, 3, 3333]])


class FileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'battery.log'

    def test_append_sync_preserve_and_rotation(self):
        log = AppendLog(self.path)
        with patch('record_battery.os.fsync', wraps=os.fsync) as sync:
            log.append('1000 30 33333')
            self.assertEqual(sync.call_count, 2)
        first = self.path.read_text()
        log.append('1030 30 33333')
        self.assertEqual(self.path.read_text(), first + '1030 30 33333\n')
        self.path.rename(self.root / 'old.log')
        log.append('1060 30 33334')
        self.assertIn('1060 30 33334\n', self.path.read_text())
        self.assertNotIn('1060', (self.root / 'old.log').read_text())

    def test_interrupted_tail_invalidated(self):
        self.path.write_text('1000 30 33')
        AppendLog(self.path).append('1030 30 33333')
        self.assertEqual(self.path.read_text(), '1000 30 33 # incomplete record\n1030 30 33333\n')

    def test_short_writes_handled(self):
        original = os.write
        with patch('record_battery.os.write', side_effect=lambda fd, data: original(fd, data[:4])):
            AppendLog(self.path).append('1030 30 33333')
        self.assertTrue(self.path.read_text().endswith('1030 30 33333\n'))

    def test_symlinks_refused(self):
        target = self.root / 'target'
        target.write_text('preserved')
        self.path.symlink_to(target)
        with self.assertRaises(OSError):
            AppendLog(self.path).append('1 1 1')
        self.assertEqual(target.read_text(), 'preserved')

    def test_sysfs_signed_readings(self):
        battery = self.root / 'bq27542-0'
        battery.mkdir()
        (battery / 'voltage_now').write_text('4254000\n')
        (battery / 'current_now').write_text('-500000\n')
        reading = read_sample(self.root)
        self.assertEqual(reading.discharge_power, 4254000 * 500000)
        (self.root / 'bq27542-1').mkdir()
        with self.assertRaises(OSError):
            read_sample(self.root)

    def test_sigterm_saves_partial_and_process_lock(self):
        # Exercise the real event loop and signals with deterministic fake readings.
        harness = '''import sys, time
sys.path.insert(0, sys.argv.pop(1))
import record_battery as r
r.read_sample = lambda: r.Sample(int(time.time()), time.clock_gettime_ns(time.CLOCK_BOOTTIME), 4000000, -1000000)
r.main()
'''
        args = [sys.executable, '-c', harness, str(Path(__file__).resolve().parent),
                '--log', str(self.path), '--lock', str(self.root / 'lock'),
                '--interval-seconds', '1', '--sample-seconds', '0.1']
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while not self.path.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(self.path.exists(), 'first interval was not saved')
            rival = subprocess.run(args, capture_output=True, text=True, timeout=3)
            self.assertNotEqual(rival.returncode, 0)
            before = self.path.read_text()
            time.sleep(0.2)
            process.send_signal(signal.SIGTERM)
            _, error = process.communicate(timeout=3)
            self.assertEqual(process.returncode, 0, error)
            rows = [line for line in self.path.read_text().splitlines() if not line.startswith('#')]
            self.assertEqual(len(rows), 2)
            self.assertTrue(self.path.read_text().startswith(before))
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


if __name__ == '__main__':
    unittest.main()
