#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
[[ $EUID == 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
for name in bq27xxx_battery bq27xxx_battery_i2c; do
    [[ -f src/$name.ko ]] || { echo "Build first: missing src/$name.ko" >&2; exit 1; }
    [[ $(modinfo -F vermagic "src/$name.ko") == "$(uname -r) "* ]] || { echo 'Kernel version mismatch; rebuild.' >&2; exit 1; }
done
restore() { modprobe bq27xxx_battery_i2c || true; }
modprobe -r bq27xxx_battery_i2c
if ! modprobe -r bq27xxx_battery; then restore; exit 1; fi
if ! insmod src/bq27xxx_battery.ko dt_monitored_battery_updates_nvm=0; then restore; exit 1; fi
if ! insmod src/bq27xxx_battery_i2c.ko; then
    modprobe -r bq27xxx_battery || true
    restore
    exit 1
fi
cat /sys/class/power_supply/bq27542-*/uevent
