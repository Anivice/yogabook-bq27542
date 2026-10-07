#!/usr/bin/env python3
"""Explicit one-shot reset experiment; probe.sh owns driver restoration."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import dataflash as df


class ResetGauge(df.Gauge):
    def reset_count(self):
        self.transfer('w3@0x55', '0x00', '0x05', '0x00')
        time.sleep(0.05)
        return int.from_bytes(self.read(0, 2), 'little')

    def reset_once(self):
        # Deliberately no retry: a failed transfer can still have reset the IC.
        self.transfer('w3@0x55', '0x00', '0x41', '0x00')


def charger_online():
    for supply in Path('/sys/class/power_supply').iterdir():
        try:
            kind = (supply / 'type').read_text().strip()
            if (kind == 'Mains' or kind.startswith('USB')) and (
                    supply / 'online').read_text().strip() == '1':
                return True
        except OSError:
            continue
    return False


def validate(gauge, require_qen=True):
    client = Path('/sys/bus/i2c/devices/i2c-bq27542')
    if not client.exists() or (client / 'name').read_text().strip() != 'bq27542':
        raise RuntimeError('expected BQ27542 client is absent')
    if client.resolve().parent.name != 'i2c-0' or (client / 'driver').exists():
        raise RuntimeError('bus-0 client must be unbound by probe.sh')
    if gauge.control_query(1) != 0x0542 or gauge.control_query(2) != 0x0201:
        raise RuntimeError('unexpected device or firmware')
    status = gauge.control_query(0)
    if status & (1 << 13):
        raise RuntimeError('gauge is SEALED; no unlock is attempted')
    print(f'CONTROL_STATUS=0x{status:04x}; QEN={status & 1}; '
          f'VOK={(status >> 1) & 1}; RUP_DIS={(status >> 2) & 1}', flush=True)
    if require_qen and not status & 1:
        raise RuntimeError('Qmax updates are disabled (QEN=0); refusing reset experiment')


REGISTERS = {
    'temperature_dK': 0x06, 'voltage_mV': 0x08, 'flags': 0x0a,
    'NAC_mAh': 0x0c, 'FAC_mAh': 0x0e, 'RM_mAh': 0x10,
    'FCC_mAh': 0x12, 'current_mA': 0x14, 'safety_status': 0x1a,
    'FCC_unfiltered_mAh': 0x1c, 'RM_unfiltered_mAh': 0x20,
    'SOC_percent': 0x2c, 'SOH_raw': 0x2e, 'DOD0': 0x36,
}


def sample(gauge):
    result = {'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
              'control_status': gauge.control_query(0)}
    for name, reg in REGISTERS.items():
        value = int.from_bytes(gauge.read(reg, 2), 'little')
        if name == 'current_mA' and value & 0x8000:
            value -= 0x10000
        result[name] = value
    print(json.dumps(result), flush=True)
    return result


def power_check(reading):
    if not charger_online():
        raise RuntimeError('no online Mains/USB supply; connect the charger')
    if not 4000 <= reading['voltage_mV'] <= 4400:
        raise RuntimeError('experiment requires battery voltage 4000..4400 mV')
    if reading['current_mA'] < -50:
        raise RuntimeError('battery is discharging; leave charger connected and idle')
    if not 2731 <= reading['temperature_dK'] <= 3181 or reading['safety_status']:
        raise RuntimeError('temperature/safety status unsuitable for experiment')


def capture(gauge):
    blocks = {}
    for cls, indices in df.BLOCKS.items():
        for index in indices:
            block, checksum = gauge.block(cls, index)
            key = f'{cls}:{index}'
            blocks[key] = block.hex()
            print(f'DF class={cls} block={index} checksum=0x{checksum:02x}: '
                  f'{block.hex(" ")}', flush=True)
    return blocks


def save(directory, name, data):
    # Exclusive creation and fsync; any failure stops before the reset.
    with (directory / name).open('x') as handle:
        json.dump(data, handle, indent=2)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def experiment(gauge):
    validate(gauge)
    print('=== before reset ===', flush=True)
    reading = sample(gauge)
    power_check(reading)
    before_count = gauge.reset_count()
    before = capture(gauge)
    directory = Path(tempfile.mkdtemp(prefix='bq27542-reset-', dir='/var/tmp'))
    save(directory, 'before.json', {'sample': reading, 'reset_count': before_count,
                                   'blocks': before})
    print(f'Backup directory: {directory}', flush=True)
    # Recheck identity, seal state and external power immediately before RESET.
    validate(gauge)
    power_check(sample(gauge))
    print('Sending one Control RESET (0x0041). No retries.', flush=True)
    gauge.reset_once()
    time.sleep(5)
    # RESET changes status flags. Collect evidence rather than gating reads on QEN/VOK.
    validate(gauge, require_qen=False)
    samples = []
    for delay in (0, 2, 8, 20):
        time.sleep(delay)
        print('=== after reset ===', flush=True)
        samples.append(sample(gauge))
    after_count = gauge.reset_count()
    after = capture(gauge)
    save(directory, 'after.json', {'samples': samples, 'reset_count': after_count,
                                  'blocks': after})
    print(f'Reset counter: {before_count} -> {after_count}', flush=True)
    changed = False
    for key in before:
        a, b = bytes.fromhex(before[key]), bytes.fromhex(after[key])
        for offset, (old, new) in enumerate(zip(a, b)):
            if old != new:
                changed = True
                cls, index = map(int, key.split(':'))
                print(f'DF change: class {cls}, offset {32*index+offset}: '
                      f'0x{old:02x} -> 0x{new:02x}', flush=True)
    if not changed:
        print('Selected diagnostic blocks unchanged.', flush=True)
    print(f'Experiment complete. Backups: {directory}', flush=True)


def main():
    if os.geteuid() != 0:
        raise RuntimeError('run through sudo ./scripts/probe.sh --reset-gauge')
    experiment(ResetGauge())


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f'Reset experiment failed; do not retry automatically: {exc}',
              file=sys.stderr)
        sys.exit(1)
