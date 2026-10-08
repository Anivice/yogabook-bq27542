#!/usr/bin/env python3
"""Append measured discharge intervals; no SOC or battery-life estimation."""
import argparse
from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import signal
import stat
import sys
import threading
import time

# (uV * uA) * nanoseconds / DENOMINATOR = micro-watt-hours.
DENOMINATOR = 3_600_000_000_000_000_000
HEADER = b'# battery-log-v1: epoch_end_s duration_s energy_used_uWh\n'


@dataclass(frozen=True)
class Sample:
    epoch: int
    elapsed_ns: int
    voltage_uV: int
    current_uA: int

    @property
    def discharge_power(self):
        return self.voltage_uV * max(0, -self.current_uA)


def read_sample(root=Path('/sys/class/power_supply')):
    batteries = list(root.glob('bq27542-*'))
    if len(batteries) != 1:
        raise OSError('expected exactly one bound bq27542 battery')
    battery = batteries[0]
    voltage = int((battery / 'voltage_now').read_text())
    current = int((battery / 'current_now').read_text())
    if voltage <= 0:
        raise ValueError('invalid voltage reading')
    # BOOTTIME includes suspend; long gaps must not be integrated as measured.
    return Sample(int(time.time()), time.clock_gettime_ns(time.CLOCK_BOOTTIME), voltage, current)


class AppendLog:
    def __init__(self, path):
        self.path = Path(path)

    def append(self, line):
        # Reopen each append so rename-and-create log rotation is supported.
        fd = os.open(self.path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o644)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise OSError('battery log must be a regular file')
            prefix = HEADER if info.st_size == 0 else b''
            if info.st_size and os.pread(fd, 1, info.st_size - 1) != b'\n':
                # Make an interrupted tail explicitly invalid as a data row.
                prefix = b' # incomplete record\n'
            payload = prefix + line.encode('ascii') + b'\n'
            while payload:
                written = os.write(fd, payload)
                if written <= 0:
                    raise OSError('short append to battery log')
                payload = payload[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


class Recorder:
    def __init__(self, log, interval_seconds=30, max_gap_seconds=5):
        self.log = log
        self.interval_ns = interval_seconds * 1_000_000_000
        self.max_gap_ns = int(max_gap_seconds * 1_000_000_000)
        self.previous = None
        self.duration_ns = 0
        self.energy_numerator = 0
        self.gap_start = None

    def flush(self):
        if not self.duration_ns:
            return
        # Integer seconds, nearest second; an observed partial interval is >=1.
        duration = max(1, (self.duration_ns + 500_000_000) // 1_000_000_000)
        energy, remainder = divmod(self.energy_numerator, DENOMINATOR)
        self.log.append(f'{self.previous.epoch} {duration} {energy}')
        self.duration_ns = 0
        self.energy_numerator = remainder  # retain sub-uWh precision across rows

    def missing(self):
        if self.previous is not None:
            self.flush()
            self.gap_start = self.previous
            self.previous = None

    def push(self, current):
        if self.gap_start is not None:
            gap = (current.elapsed_ns - self.gap_start.elapsed_ns) / 1e9
            self.log.append(f'# gap {self.gap_start.epoch} {current.epoch} {gap:.3f} unavailable')
            self.gap_start = None
        if self.previous is not None:
            dt = current.elapsed_ns - self.previous.elapsed_ns
            if dt <= 0:
                raise ValueError('elapsed clock did not advance')
            if dt > self.max_gap_ns:
                self.flush()
                self.log.append(f'# gap {self.previous.epoch} {current.epoch} {dt/1e9:.3f} sampling')
            else:
                # Integrate only negative current; charging produces zero use.
                # Split a sign change at the linearly interpolated zero crossing.
                a, b = self.previous, current
                if a.current_uA < 0 < b.current_uA:
                    area = a.discharge_power * (-a.current_uA) * dt // (2 * (b.current_uA - a.current_uA))
                elif b.current_uA < 0 < a.current_uA:
                    area = b.discharge_power * (-b.current_uA) * dt // (2 * (a.current_uA - b.current_uA))
                else:
                    area = (a.discharge_power + b.discharge_power) * dt // 2
                self.energy_numerator += area
                self.duration_ns += dt
        self.previous = current
        if self.duration_ns >= self.interval_ns:
            self.flush()

    def finish(self):
        self.flush()
        if self.gap_start is not None:
            self.log.append(f'# gap {self.gap_start.epoch} {int(time.time())} unknown unfinished')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interval-seconds', type=int,
                        default=os.environ.get('BQ_RECORD_INTERVAL_SECONDS', '30'))
    parser.add_argument('--sample-seconds', type=float,
                        default=os.environ.get('BQ_SAMPLE_INTERVAL_SECONDS', '1'))
    parser.add_argument('--log', type=Path, default=Path('/var/log/battery.log'))
    parser.add_argument('--lock', type=Path,
                        default=Path('/run/yogabook-bq27542-record/record.lock'))
    args = parser.parse_args()
    if not 1 <= args.interval_seconds <= 86400 or not 0.1 <= args.sample_seconds <= 5:
        parser.error('record interval must be 1..86400 s; sample interval 0.1..5 s')
    if args.interval_seconds < args.sample_seconds:
        parser.error('record interval must be at least the sample interval')
    args.lock.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    with args.lock.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, lambda signum, frame: stop.set())
        recorder = Recorder(AppendLog(args.log), args.interval_seconds,
                            max(5, 3 * args.sample_seconds))
        unavailable = False
        try:
            while not stop.is_set():
                try:
                    reading = read_sample()
                except (OSError, ValueError) as exc:
                    recorder.missing()
                    if not unavailable:
                        print(f'Battery measurement unavailable: {exc}', file=sys.stderr, flush=True)
                    unavailable = True
                else:
                    recorder.push(reading)
                    unavailable = False
                stop.wait(args.sample_seconds)
            # Capture the final partial interval on an orderly service stop.
            try:
                final = read_sample()
            except (OSError, ValueError):
                recorder.missing()
            else:
                recorder.push(final)
        finally:
            recorder.finish()


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as exc:
        print(f'Battery recorder failed: {exc}', file=sys.stderr)
        sys.exit(1)
