#!/usr/bin/env python3
"""Explicit, backed-up Load Mode comparison; probe.sh owns binding and lock."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import reset_gauge as rg

BACKUP = Path('/var/lib/yogabook-bq27542/load-mode-backup.json')
IDENTITY = {'device': 0x0542, 'firmware': 0x0201, 'client': 'i2c-bq27542'}


def emit(event, **values):
    print(json.dumps(dict(event=event, **values)), flush=True)


def checksum(block):
    return 255 - (sum(block) & 255)


def other_bytes(block):
    return block[:1] + block[2:]


class ModeGauge(rg.ResetGauge):
    def set_mode(self, expected, mode):
        if len(expected) != 32 or expected[0] != 1 or mode not in (0, 1):
            raise ValueError('only Load Select=1, Load Mode=0/1 supported')
        current, _ = self.block(80, 0)
        if current != expected:
            raise RuntimeError('IT Cfg block changed before write; refusing')
        desired = bytearray(current)
        desired[1] = mode
        # block() left class 80/block 0 selected. Only the mode byte and its
        # block checksum are written; never overwrite the other 31 bytes.
        self.transfer('w2@0x55', '0x41', hex(mode))
        self.transfer('w2@0x55', '0x60', hex(checksum(desired)))
        time.sleep(0.5)
        actual, _ = self.block(80, 0)
        if actual != bytes(desired):
            raise RuntimeError('Load Mode write verification failed')
        return actual


def save_backup(path, block, blocks, reading):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    record = dict(format=1, identity=IDENTITY, original_block=block.hex(),
                  sha256=hashlib.sha256(block).hexdigest(),
                  original_mode=block[1], blocks=blocks, sample=reading)
    # Never reuse or overwrite a backup, including after a successful run.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(record, output, indent=2)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    # Persist a newly created directory entry as well.
    fd = os.open(path.parent.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_backup(path):
    record = json.loads(path.read_text())
    block = bytes.fromhex(record['original_block'])
    if (record.get('format') != 1 or record.get('identity') != IDENTITY or
            len(block) != 32 or block[0] != 1 or block[1] != 1 or
            record.get('original_mode') != 1 or
            record.get('sha256') != hashlib.sha256(block).hexdigest()):
        raise RuntimeError('invalid/incompatible backup; refusing write')
    return block


def charger_connected():
    return (Path("/sys/class/power_supply/bq25890-charger-0") / "online").read_text().strip() == "1"


def check_power(gauge, require_qen=True):
    if not charger_connected():
        raise RuntimeError("BQ25890 charger is offline; connect the cable")
    rg.validate(gauge, require_qen=require_qen)
    reading = rg.sample(gauge)
    rg.power_check(reading)
    return reading


def activate(gauge, mode, require_qen=True):
    # Reset once, with no retry even on a transfer error.
    gauge.reset_once()
    time.sleep(5)
    rg.validate(gauge, require_qen=False)
    status = gauge.control_query(0)
    if (status >> 3) & 1 != mode:
        raise RuntimeError(f'active LDMD bit does not match Load Mode {mode}')
    if require_qen and not status & 1:
        raise RuntimeError('QEN cleared after reset; result is inconclusive')
    emit('active_mode_verified', mode=mode, control_status=status)


class RestorationOutput:
    """A vanished log consumer must not prevent hardware restoration."""
    def __init__(self, output):
        self.output = output

    def write(self, text):
        with contextlib.suppress(BrokenPipeError):
            self.output.write(text)
        return len(text)

    def flush(self):
        with contextlib.suppress(BrokenPipeError):
            self.output.flush()


def restore(gauge, original):
    with contextlib.redirect_stdout(RestorationOutput(sys.stdout)):
        restore_checked(gauge, original)


def restore_checked(gauge, original):
    check_power(gauge, require_qen=False)
    current, _ = gauge.block(80, 0)
    if other_bytes(current) != other_bytes(original) or current[1] not in (0, 1):
        raise RuntimeError('other IT Cfg bytes changed; refusing blind restoration')
    if current != original:
        gauge.set_mode(current, original[1])
    activate(gauge, original[1], require_qen=False)
    restored, _ = gauge.block(80, 0)
    if restored != original:
        raise RuntimeError('original IT Cfg block not restored')
    with contextlib.suppress(BrokenPipeError):
        emit('original_mode_restored', mode=original[1])
        rg.sample(gauge)


def interrupted(signum, frame):
    raise InterruptedError(f'interrupted by signal {signum}')


def experiment(gauge, backup=BACKUP):
    reading = check_power(gauge)
    original, _ = gauge.block(80, 0)
    if not gauge.control_query(0) & (1 << 3):
        raise RuntimeError('active mode is not constant power; refusing comparison')
    if original[0:2] != b'\x01\x01':
        raise RuntimeError('expected Load Select=1 and Load Mode=1; refusing experiment')
    blocks = rg.capture(gauge)
    if blocks['80:0'] != original.hex():
        raise RuntimeError('IT Cfg changed while backing up; refusing')
    save_backup(backup, original, blocks, reading)
    emit('backup_saved', path=str(backup), original_mode=1)
    check_power(gauge)
    armed = False
    try:
        # A failed transfer may already have reached hardware. Arm BEFORE it.
        armed = True
        gauge.set_mode(original, 0)
        activate(gauge, 0)
        # Bounded test: roughly one minute of sequential raw samples, on AC.
        for delay in (0, 5, 10, 20, 30):
            time.sleep(delay)
            reading = rg.sample(gauge)
            rg.power_check(reading)
        rg.capture(gauge)
    finally:
        if armed:
            # Allow one restoration attempt to finish despite repeated Ctrl-C.
            previous = {s: signal.signal(s, signal.SIG_IGN)
                        for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
            try:
                restore(gauge, original)
            except BaseException:
                print(f'RESTORATION FAILED. Keep charger connected. Run: '
                      f'sudo ./scripts/probe.sh --restore-load-mode {backup}',
                      file=sys.stderr)
                raise
            finally:
                for s, handler in previous.items():
                    signal.signal(s, handler)
    emit('experiment_complete', backup=str(backup))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--restore', type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError('run through sudo ./scripts/probe.sh --load-mode-experiment')
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupted)
    gauge = ModeGauge()
    if args.restore:
        original = read_backup(args.restore)
        restore(gauge, original)
    else:
        experiment(gauge)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        print(f'Load Mode experiment failed; do not retry automatically: {exc}', file=sys.stderr)
        sys.exit(1)
