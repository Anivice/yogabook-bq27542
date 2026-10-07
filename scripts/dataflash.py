#!/usr/bin/env python3
"""BQ27542-G1 data-flash reader. Run through probe.sh --dataflash only."""
import os
from pathlib import Path
import subprocess
import sys
import time

# Only diagnostic classes; never the security/key class.
BLOCKS = {48: (0,), 64: (0,), 80: (0, 1, 2, 3), 82: (0,),
          88: (0,), 89: (0,), 104: (0,)}

class Gauge:
    def transfer(self, *args):
        result = subprocess.run(['i2ctransfer', '-y', '0', *args], check=True,
                                capture_output=True, text=True, timeout=5)
        return result.stdout

    def read(self, reg, length):
        words = self.transfer('w1@0x55', hex(reg), f'r{length}').split()
        if len(words) != length or any(len(w) != 4 or not w.startswith('0x') for w in words):
            raise RuntimeError(f'malformed reply for 0x{reg:02x}')
        return bytes(int(w, 16) for w in words)

    def control_query(self, query):
        if query not in (0, 1, 2):
            raise ValueError('query not whitelisted')
        self.transfer('w3@0x55', '0x00', hex(query & 255), hex(query >> 8))
        time.sleep(0.05)
        return int.from_bytes(self.read(0, 2), 'little')

    def select(self, reg, value):
        allowed = ((reg == 0x61 and value == 0) or
                   (reg == 0x3e and value in BLOCKS) or
                   (reg == 0x3f and value in range(4)))
        if not allowed:
            raise ValueError('selection write not whitelisted')
        self.transfer('w2@0x55', hex(reg), hex(value))
        time.sleep(0.02)

    def block(self, subclass, index):
        if subclass not in BLOCKS or index not in BLOCKS[subclass]:
            raise ValueError('block not whitelisted')
        for _ in range(3):
            self.select(0x61, 0)
            self.select(0x3e, subclass)
            self.select(0x3f, index)
            a = self.read(0x40, 32)
            ca = self.read(0x60, 1)[0]
            b = self.read(0x40, 32)
            cb = self.read(0x60, 1)[0]
            if a == b and ca == cb == (255 - (sum(a) & 255)):
                return a, ca
            time.sleep(0.05)
        raise RuntimeError(f'unstable data/checksum mismatch: class {subclass}, block {index}')


def value(data, offset, size=2, signed=False):
    return int.from_bytes(data[offset:offset+size], 'big', signed=signed)


def decode(data):
    # SLUUB65B Table 16-3 offsets. Raw blocks are also printed because the
    # manual contains inconsistent offsets in some narrative examples.
    fields = [
        (48, 0, 2, True, 'Design Voltage', 'mV'),
        (48, 12, 2, True, 'Design Capacity', 'mAh'),
        (48, 14, 2, True, 'Design Energy', 'raw energy units'),
        (48, 16, 2, True, 'SOH Load I', 'mA'),
        (48, 23, 1, False, 'Design Energy Scale', ''),
        (64, 0, 2, False, 'Pack Configuration', 'raw'),
        (64, 2, 1, False, 'Pack Configuration B', 'raw'),
        (64, 3, 1, False, 'Pack Configuration C', 'raw'),
        (64, 4, 1, False, 'Pack Configuration D', 'raw'),
        (80, 0, 1, False, 'Load Select', ''),
        (80, 1, 1, False, 'Load Mode', ''),
        (80, 64, 2, True, 'Terminate Voltage', 'mV'),
        (80, 66, 2, True, 'Term V Delta', 'mV'),
        (80, 73, 2, True, 'User Rate-mA', 'mA'),
        (80, 75, 2, True, 'User Rate-Pwr', 'raw power units'),
        (80, 77, 2, True, 'Reserve Cap-mAh', 'mAh'),
        (80, 79, 2, True, 'Reserve Energy', 'raw energy units'),
        (80, 88, 1, False, 'Max Sim Rate', 'hourrate'),
        (80, 89, 1, False, 'Min Sim Rate', 'hourrate'),
        (80, 92, 2, True, 'Trace Resistance', 'mOhm'),
        (80, 94, 2, True, 'Downstream Resistance', 'mOhm'),
        (82, 0, 2, True, 'Qmax Cell 0', 'mAh'),
        (82, 2, 1, False, 'Update Status', 'raw'),
        (82, 3, 2, True, 'V at Chg Term', 'mV'),
        (82, 5, 2, True, 'Avg I Last Run', 'mA'),
        (82, 7, 2, True, 'Avg P Last Run', 'raw power units'),
        (82, 9, 2, True, 'Delta Voltage', 'mV'),
    ]
    print('=== decoded fields (TRM Table 16-3; data-flash words are big-endian) ===')
    for cls, off, size, signed, name, unit in fields:
        v = value(data[cls], off, size, signed)
        print(f'{name:28s} {v:8d} {unit:18s} [class {cls}, offset {off}]')
    for cls in (88, 89):
        print(f'Ra class {cls}: flag=0x{value(data[cls], 0):04x}; '
              'Ra[0..14] raw units of 2^-10 ohm: ' +
              ', '.join(str(value(data[cls], 2+2*i, signed=True)) for i in range(15)))
    print('Calibration class 104 retained as raw bytes; TI F4 is not IEEE float.')


def main():
    if os.geteuid() != 0:
        raise RuntimeError('run through sudo ./scripts/probe.sh --dataflash')
    client = Path('/sys/bus/i2c/devices/i2c-bq27542')
    if not client.exists() or (client/'name').read_text().strip() != 'bq27542':
        raise RuntimeError('expected BQ27542 client is absent')
    if client.resolve().parent.name != 'i2c-0' or (client/'driver').exists():
        raise RuntimeError('expected bus 0 client must be unbound by probe.sh')
    gauge = Gauge()
    if gauge.control_query(1) != 0x0542 or gauge.control_query(2) != 0x0201:
        raise RuntimeError('unexpected device/firmware; refusing data-flash decode')
    status = gauge.control_query(0)
    print(f'=== data-flash status: 0x{status:04x}; SS={(status>>13)&1}, FAS={(status>>14)&1} ===', flush=True)
    if status & (1 << 13):
        raise RuntimeError('gauge is SEALED; no unlock is attempted')
    data = {}
    for cls, indices in BLOCKS.items():
        chunks = []
        for index in indices:
            block, checksum = gauge.block(cls, index)
            print(f'DF class={cls} block={index} checksum=0x{checksum:02x}: {block.hex(" ")}', flush=True)
            chunks.append(block)
        data[cls] = b''.join(chunks)
    decode(data)
    print('Dump complete. No BlockData or checksum writes were sent.')

if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f'Data-flash dump failed: {exc}', file=sys.stderr)
        sys.exit(1)
