#!/usr/bin/env bash
set -euo pipefail
[[ $EUID == 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
modprobe -r bq27xxx_battery_i2c
modprobe -r bq27xxx_battery
modprobe bq27xxx_battery_i2c
