#!/usr/bin/env python3
"""Read the bound gauge through sysfs; emit flushed JSON Lines to stdout."""
import argparse
import json
from pathlib import Path
import sys
import time
from datetime import datetime, timezone


def emit(record):
    print(json.dumps(record, separators=(',', ':')), flush=True)


def integrate(previous, current):
    dt = current['monotonic_s'] - previous['monotonic_s']
    # Only integrate known intervals on battery, without interpolating outages.
    if previous['online'] or current['online']:
        return 0.0, 0.0, 0.0
    a, b = max(0, -previous['current_uA']), max(0, -current['current_uA'])
    mah = (a + b) / 2 * dt / 3600000
    wh = (a * previous['voltage_uV'] + b * current['voltage_uV']) / 2 * dt / 3.6e15
    return mah, wh, dt


def read_sample(battery, charger):
    def integer(path, name):
        return int((path / name).read_text().strip())
    return {'utc': datetime.now(timezone.utc).isoformat(),
            'monotonic_s': time.monotonic(),
            'voltage_uV': integer(battery, 'voltage_now'),
            'current_uA': integer(battery, 'current_now'),
            'temp_dC': integer(battery, 'temp'),
            'gauge_percent': integer(battery, 'capacity'),
            'gauge_full_uAh': integer(battery, 'charge_full'),
            'gauge_now_uAh': integer(battery, 'charge_now'),
            'online': integer(charger, 'online'),
            'charger_status': (charger / 'status').read_text().strip()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interval', type=float, default=1)
    parser.add_argument('--stop-voltage-mv', type=int, default=3250)
    parser.add_argument('--samples', type=int, default=0, help='0 means unlimited')
    args = parser.parse_args()
    if not 0.5 <= args.interval <= 10 or not 3250 <= args.stop_voltage_mv <= 4000 or args.samples < 0:
        parser.error('interval 0.5..10; stop voltage 3250..4000; samples >=0')
    batteries = list(Path('/sys/class/power_supply').glob('bq27542-*'))
    chargers = list(Path('/sys/class/power_supply').glob('bq25890-charger-*'))
    if len(batteries) != 1 or len(chargers) != 1:
        raise RuntimeError('expected exactly one bq27542 battery and bq25890 charger')
    battery, charger = batteries[0], chargers[0]
    emit({'event': 'start', 'battery': str(battery), 'charger': str(charger),
          'interval_s': args.interval, 'stop_voltage_mV': args.stop_voltage_mv,
          'design_mAh': int((battery / 'charge_full_design').read_text()) / 1000,
          'note': 'Measured discharge totals, not a calibrated SOC/health estimate.'})
    previous = None
    mah = wh = measured = unmeasured = 0.0
    low = sequence = 0
    anchor = False
    missing = False
    while True:
        try:
            now = read_sample(battery, charger)
        except (OSError, ValueError) as exc:
            emit({'event': 'read_error', 'utc': datetime.now(timezone.utc).isoformat(), 'error': str(exc)})
            missing = True
            # Retain previous time so the eventual gap can be reported.
            time.sleep(args.interval)
            continue
        if previous is None:
            anchor = now['online'] == 1 and now['charger_status'] == 'Full'
            emit({'event': 'full_anchor', 'charger_full_at_start': anchor})
        else:
            dt = now['monotonic_s'] - previous['monotonic_s']
            if not missing and dt <= max(5, 3 * args.interval):
                dm, dw, ds = integrate(previous, now)
                mah += dm
                wh += dw
                measured += ds
            else:
                unmeasured += dt
                emit({'event': 'gap', 'unmeasured_s': dt})
            if previous['online'] != now['online']:
                emit({'event': 'power_change', 'online': now['online']})
                if now['online']:
                    emit({'event': 'end', 'reason': 'charger_reconnected',
                          'discharged_mAh': mah, 'discharged_Wh': wh,
                          'measured_discharge_s': measured, 'unmeasured_s': unmeasured,
                          'full_anchor': anchor})
                    return
        missing = False
        sequence += 1
        now.update(event='sample', sequence=sequence, discharged_mAh=round(mah, 6),
                   discharged_Wh=round(wh, 6), measured_discharge_s=round(measured, 3),
                   unmeasured_s=round(unmeasured, 3))
        emit(now)
        low = low + 1 if not now['online'] and now['voltage_uV'] <= args.stop_voltage_mv * 1000 else 0
        if low >= 3 or (args.samples and sequence >= args.samples):
            emit({'event': 'end', 'reason': 'voltage_endpoint' if low >= 3 else 'sample_limit',
                  'discharged_mAh': mah, 'discharged_Wh': wh,
                  'measured_discharge_s': measured, 'unmeasured_s': unmeasured,
                  'full_anchor': anchor})
            return
        previous = now
        time.sleep(args.interval)


if __name__ == '__main__':
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        # Prevent a second BrokenPipeError while Python flushes at shutdown.
        import os
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(1)
    except (RuntimeError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
