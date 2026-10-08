#!/usr/bin/env python3
"""Journal-based usable energy estimate, with charger Full as the full anchor."""
import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time

SEED_UWH = 8_989_091  # Supplied 2026-10-08 log: observed discharge, provisional seed.
STATUSES = {'Charging', 'Full', 'Discharging', 'Not_charging', 'Unknown'}


def atomic_write(path, text, durable=False):
    """Replace a complete file, never follow a pre-existing /tmp target symlink."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o644)
            stream.write(text)
            stream.flush()
            if durable:
                os.fsync(stream.fileno())
        os.replace(temporary, path)
        if durable:
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Model:
    def __init__(self, seed=SEED_UWH, efficiency=0.9, empty_uV=3_250_000):
        self.full = float(seed)
        self.current = None
        self.efficiency = efficiency
        self.empty_uV = empty_uV
        self.uncertain = True
        self.status = 'Unknown'
        self.full_cycle = None
        self.low_cycle = None
        self.candidate = None
        self.capacity_cycles = 0
        self.efficiency_cycles = 0
        self.last_epoch = None
        self.legacy_out = 0
        self.gaps = 0

    def gap(self):
        self.uncertain = True
        self.gaps += 1
        for cycle in (self.full_cycle, self.low_cycle):
            if cycle is not None:
                cycle['valid'] = False
        self.candidate = None

    @staticmethod
    def cycle():
        return {'out': 0, 'in': 0, 'valid': True}

    def set_status(self, status):
        if status not in STATUSES:
            raise ValueError('invalid charger state')
        previous = self.status
        self.status = status
        if status != 'Full' or previous == 'Full':
            return
        low = self.low_cycle
        full = self.full_cycle
        observed = None
        if low and low['valid'] and low['in'] > 0:
            # A previous Full->low measurement can calibrate charge losses.
            if self.candidate is not None:
                ratio = (self.candidate + low['out']) / low['in']
                if 0.5 <= ratio <= 1:
                    self.efficiency = 0.8 * self.efficiency + 0.2 * ratio
                    self.efficiency_cycles += 1
                    observed = self.candidate
            else:
                observed = self.efficiency * low['in'] - low['out']
            # Reject grossly implausible cycles; learn gradually rather than
            # replacing a learned full capacity with one noisy voltage endpoint.
            if observed is not None and 0.25 * self.full <= observed <= 4 * self.full:
                self.full = 0.8 * self.full + 0.2 * observed
                self.capacity_cycles += 1
        elif full and full['valid'] and full['in'] > 0 and full['out'] >= 0.1 * self.full:
            # Full->Full partial round trips calibrate efficiency, not capacity.
            ratio = full['out'] / full['in']
            if 0.5 <= ratio <= 1:
                self.efficiency = 0.8 * self.efficiency + 0.2 * ratio
                self.efficiency_cycles += 1
        self.current = self.full
        self.uncertain = False
        self.full_cycle = self.cycle()
        self.low_cycle = None
        self.candidate = None

    def interval(self, epoch, duration, discharged, metadata=None):
        if self.last_epoch is not None and epoch > self.last_epoch + duration + 3:
            self.gap()
        self.last_epoch = epoch
        if metadata is None:
            # Old zero rows carry no charging or anchor information.
            self.legacy_out += discharged
            self.gap()
            if self.current is not None:
                self.current -= discharged
            return
        charged, voltage, current, status = metadata
        if status == 'Unknown':
            self.gap()
        net = self.efficiency * charged - discharged
        if self.current is not None:
            # Keep the internal value unclipped: exceeding an anchor reveals
            # model error rather than losing debt/credit on every interval.
            self.current += net
        for cycle in (self.full_cycle, self.low_cycle):
            if cycle is not None:
                cycle['out'] += discharged
                cycle['in'] += charged
        if voltage <= self.empty_uV and current < 0 and self.low_cycle is None:
            # This is a configured usable-voltage endpoint, not proof of cell
            # exhaustion. Ignore an early loaded-voltage sag after a full anchor.
            delivered = None
            if self.full_cycle is not None:
                delivered = self.full_cycle['out'] - self.efficiency * self.full_cycle['in']
                if delivered < 0.25 * self.full:
                    return
            if self.full_cycle and self.full_cycle['valid']:
                self.candidate = delivered
            self.full_cycle = None
            self.low_cycle = self.cycle()
            self.current = 0.0
            self.uncertain = False

    def initialize_current(self, status, voltage):
        if self.current is None:
            if status == 'Full':
                self.set_status('Unknown')
                self.set_status('Full')
            elif voltage is not None:
                # Only a startup fallback when no saved/recorded anchor exists.
                fraction = (voltage - self.empty_uV) / (4_350_000 - self.empty_uV)
                self.current = self.full * min(1, max(0, fraction))
                self.uncertain = True
            else:
                self.current = 0.0
                self.uncertain = True

    def values(self, live_status=None):
        current = self.full if live_status == 'Full' else (self.current or 0)
        return round(self.full), round(min(self.full, max(0, current)))


class Journal:
    """Read new complete lines every refresh, preserving byte position on restart."""
    def __init__(self, model):
        self.model = model
        self.identity = None
        self.offset = 0
        self.pending = None
        self.invalid_lines = 0

    def line(self, line):
        parts = line.split()
        try:
            if parts[:2] == ['#', 'interval-v2']:
                if len(parts) != 8:
                    raise ValueError('metadata length')
                epoch, duration, charged, voltage, current = map(int, parts[2:7])
                if epoch < 0 or not 1 <= duration <= 86401 or charged < 0 or voltage <= 0 or parts[7] not in STATUSES:
                    raise ValueError('invalid metadata')
                self.pending = (epoch, duration, charged, voltage, current, parts[7])
                return
            if parts[:2] == ['#', 'state']:
                if len(parts) != 4 or int(parts[2]) < 0:
                    raise ValueError('state length')
                self.model.set_status(parts[3])
                self.model.last_epoch = int(parts[2])
            elif parts[:2] in (['#', 'gap'], ['#', 'session']):
                self.model.gap()
                if parts[:2] == ['#', 'session']:
                    self.model.status = 'Unknown'
                    self.model.last_epoch = None
                self.pending = None
            elif line.startswith('#'):
                return
            else:
                if len(parts) != 3 or not all(part.isascii() and part.isdigit() for part in parts):
                    raise ValueError('invalid data row')
                epoch, duration, discharged = map(int, parts)
                if duration < 1 or duration > 86401:
                    raise ValueError('invalid duration')
                metadata = None
                if self.pending is not None and self.pending[:2] == (epoch, duration):
                    metadata = self.pending[2:]
                self.pending = None
                self.model.interval(epoch, duration, discharged, metadata)
        except ValueError:
            self.invalid_lines += 1
            self.pending = None
            self.model.gap()

    def read(self, path):
        with Path(path).open('rb') as stream:
            info = os.fstat(stream.fileno())
            identity = [info.st_dev, info.st_ino]
            if self.identity is not None and (identity != self.identity or info.st_size < self.offset):
                self.model.gap()
                self.offset = 0
                self.pending = None
            self.identity = identity
            stream.seek(self.offset)
            while True:
                line = stream.readline()
                if not line or not line.endswith(b'\n'):
                    break  # leave partial writes for the next refresh
                self.offset = stream.tell()
                self.line(line.decode('ascii', errors='replace').strip())

    def save(self, path):
        data = {'version': 1, 'model': self.model.__dict__,
                'journal': {key: getattr(self, key) for key in ('identity', 'offset', 'pending', 'invalid_lines')}}
        atomic_write(path, json.dumps(data, allow_nan=False) + '\n', durable=True)

    @classmethod
    def restore(cls, path, model):
        if not Path(path).exists():
            return cls(model)
        data = json.loads(Path(path).read_text())
        if data['version'] != 1:
            raise ValueError('unsupported estimator checkpoint')
        configured_empty = model.empty_uV
        model.__dict__.update(data['model'])
        if model.empty_uV != configured_empty:
            model.empty_uV = configured_empty
            model.gap()
        if not math.isfinite(model.full) or model.full <= 0 or not 0.5 <= model.efficiency <= 1:
            raise ValueError('invalid estimator checkpoint')
        journal = cls(model)
        for key in ('identity', 'offset', 'pending', 'invalid_lines'):
            setattr(journal, key, data['journal'][key])
        if journal.pending is not None:
            journal.pending = tuple(journal.pending)
        return journal


def live_state(root):
    try:
        status = (root / 'bq25890-charger-0/status').read_text().strip().replace(' ', '_')
        if status not in STATUSES:
            status = 'Unknown'
    except OSError:
        status = 'Unknown'
    try:
        batteries = list(root.glob('bq27542-*'))
        voltage = int((batteries[0] / 'voltage_now').read_text()) if len(batteries) == 1 else None
        if voltage is not None and voltage <= 0:
            voltage = None
    except (OSError, ValueError):
        voltage = None
    return status, voltage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--log', type=Path, default=Path('/var/log/battery.log'))
    parser.add_argument('--state', type=Path, default=Path('/var/lib/yogabook-bq27542/estimate-state.json'))
    parser.add_argument('--output-dir', type=Path, default=Path('/tmp'))
    parser.add_argument('--sysfs', type=Path, default=Path('/sys/class/power_supply'))
    parser.add_argument('--initial-full-uwh', type=int, default=os.environ.get('BQ_INITIAL_FULL_UWH', str(SEED_UWH)))
    parser.add_argument('--efficiency', type=float, default=os.environ.get('BQ_CHARGE_EFFICIENCY', '0.9'))
    parser.add_argument('--empty-uv', type=int, default=os.environ.get('BQ_EMPTY_UV', '3250000'))
    args = parser.parse_args()
    if args.initial_full_uwh <= 0 or not 0.5 <= args.efficiency <= 1 or not 2_500_000 <= args.empty_uv < 4_000_000:
        parser.error('invalid seed, efficiency, or empty-voltage endpoint')
    args.state.parent.mkdir(parents=True, exist_ok=True)
    with (args.state.parent / 'estimate.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        journal = Journal.restore(args.state, Model(args.initial_full_uwh, args.efficiency, args.empty_uv))
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, lambda signum, frame: stop.set())
        previous_status = None
        next_refresh = 0
        read_error = False
        while not stop.is_set():
            tick = time.monotonic()
            status, voltage = live_state(args.sysfs)  # once per second, including while charging
            if tick >= next_refresh or status != previous_status:
                try:
                    journal.read(args.log)
                    read_error = False
                except OSError as exc:
                    journal.model.gap()
                    if not read_error:
                        print(f'Journal unavailable: {exc}', file=sys.stderr, flush=True)
                    read_error = True
                journal.model.initialize_current(status, voltage)
                full, current = journal.model.values(status)
                # A live Full anchor corrects stale historical remaining energy;
                # the recorder's state event owns cycle calibration/order.
                if status == 'Full':
                    journal.model.current = journal.model.full
                    journal.model.uncertain = False
                if status == 'Unknown':
                    journal.model.uncertain = True
                atomic_write(args.output_dir / 'bat_full', f'{full}\n')
                atomic_write(args.output_dir / 'bat_cur', f'{current}\n')
                journal.save(args.state)
                print(f'full_uWh={full} current_uWh={current} charger={status} '
                      f'voltage_uV={voltage} uncertain={journal.model.uncertain}', flush=True)
                next_refresh = tick + 10
            previous_status = status
            stop.wait(max(0, 1 - (time.monotonic() - tick)))
        journal.read(args.log)
        journal.save(args.state)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'Battery estimator failed: {exc}', file=sys.stderr)
        sys.exit(1)
