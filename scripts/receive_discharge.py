#!/usr/bin/env python3
"""Persist complete JSON records received on stdin, fsync every record."""
import json
import os
import sys

if len(sys.argv) != 2:
    raise SystemExit('Usage: ncat -l --recv-only 5000 | python3 receive_discharge.py NEW_LOG.jsonl')
try:
    with open(sys.argv[1], 'xb') as output:
        for line in sys.stdin.buffer:
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError):
                print('Ignoring incomplete/invalid final record.', file=sys.stderr)
                continue
            output.write(line.rstrip(b'\r\n') + b'\n')
            output.flush()
            os.fsync(output.fileno())
            if record.get('event') == 'sample':
                print(f"{record['utc']}  {record['voltage_uV']/1e6:.3f} V  "
                      f"{record['current_uA']/1000:.0f} mA  "
                      f"{record['discharged_mAh']:.1f} mAh  "
                      f"{record['discharged_Wh']:.3f} Wh", flush=True)
            else:
                print(json.dumps(record), flush=True)
except KeyboardInterrupt:
    pass
