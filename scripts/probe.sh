#!/usr/bin/env bash
# BQ27542 diagnostic probe for the Yoga Book's bus 0 / address 0x55.
set -euo pipefail
dataflash=0
reset_gauge=0
if [[ ${1:-} == --dataflash || ${1:-} == --reset-gauge ]]; then
    [[ $1 != --reset-gauge ]] || reset_gauge=1
    dataflash=1
    shift
    command -v python3 >/dev/null || { echo 'Install python3 first.' >&2; exit 1; }
    helper="$(dirname -- "${BASH_SOURCE[0]}")/dataflash.py"
    if (( reset_gauge )); then helper="$(dirname -- "${BASH_SOURCE[0]}")/reset_gauge.py"; fi
    [[ -r $helper ]] || { echo 'Missing diagnostic helper.' >&2; exit 1; }
fi
count=${1:-1}
interval=${2:-10}
if [[ $# -gt 2 || ! $count =~ ^[1-9][0-9]*$ || ! $interval =~ ^[1-9][0-9]*$ ]] ||
   (( count > 3600 || interval > 3600 )); then
    echo 'Usage: sudo ./scripts/probe.sh [--dataflash|--reset-gauge] [samples:1..3600] [interval-seconds:1..3600]' >&2
    exit 2
fi
[[ $EUID == 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
command -v i2ctransfer >/dev/null || { echo 'Install i2c-tools first.' >&2; exit 1; }
command -v flock >/dev/null || { echo 'Install util-linux (flock) first.' >&2; exit 1; }
# Coordinate probe instances before changing the driver binding.
exec 9>/run/lock/yogabook-bq27542-probe.lock
flock -n 9 || { echo 'Another probe is running.' >&2; exit 1; }
modprobe i2c-dev
DRV=/sys/bus/i2c/drivers/bq27xxx-battery
DEV=i2c-bq27542
# Resolve the client independently of its current driver binding.
CLIENT=/sys/bus/i2c/devices/$DEV
path=$(readlink -e "$CLIENT") || {
    echo "I2C client $DEV is absent; check the platform module that creates it." >&2
    exit 1
}
[[ -r $path/name && $(cat "$path/name") == bq27542 && $(basename "$(dirname "$path")") == i2c-0 ]] || {
    echo 'Expected bq27542 client on i2c-0; refusing to probe another device.' >&2
    exit 1
}
[[ -d $DRV && -w $DRV/bind && -w $DRV/unbind ]] || {
    echo 'bq27xxx-battery driver is unavailable; load the battery modules first.' >&2
    exit 1
}
if [[ -L $path/driver ]]; then
    [[ $(readlink -e "$path/driver") == $(readlink -e "$DRV") ]] || {
        echo "Client is bound to another driver: $(readlink -e "$path/driver")" >&2
        exit 1
    }
else
    echo 'Client is unbound; restoring bq27xxx-battery before probing.' >&2
    printf '%s\n' "$DEV" > "$DRV/bind" || {
        echo 'Failed to restore the driver; inspect kernel logs.' >&2
        exit 1
    }
fi
[[ -L $DRV/$DEV ]] || {
    echo 'Binding did not create the expected driver link; inspect kernel logs.' >&2
    exit 1
}
unbound=0
cleanup() {
    local status=$?
    trap - EXIT
    # A stopped tee/SSH consumer must not interrupt driver restoration.
    trap '' PIPE
    if (( unbound )) && [[ ! -L $DRV/$DEV ]]; then
        if ! printf '%s\n' "$DEV" > "$DRV/bind"; then
            echo "Rebind failed. Recover with: echo $DEV | sudo tee $DRV/bind" >&2 || true
            status=1
        else
            echo '=== kernel driver rebound ===' >&2 || true
        fi
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
snapshot() {
    local file
    for file in "$path"/power_supply/*/uevent; do
        [[ ! -r $file ]] || cat "$file"
    done
}
read16() {
    local output lo hi extra
    output=$(i2ctransfer -y 0 w1@0x55 "$1" r2)
    read -r lo hi extra <<< "$output"
    [[ $lo =~ ^0x[0-9a-fA-F]{2}$ && $hi =~ ^0x[0-9a-fA-F]{2}$ && -z $extra ]] || {
        echo "Malformed I2C reply for register $1: $output" >&2
        return 1
    }
    printf '%d\n' "$((lo | (hi << 8)))"
}
control() {
    # Whitelist identity/status queries. Never accept arbitrary Control writes.
    case "$1" in 0x0000|0x0001|0x0002|0x0003|0x0008|0x000c) ;;
        *) echo 'Unsupported control query' >&2; return 1 ;;
    esac
    i2ctransfer -y 0 w3@0x55 0x00 "$(( $1 & 255 ))" "$(( ($1 >> 8) & 255 ))" >/dev/null
    sleep 0.05
    read16 0x00
}
row() {
    local value
    value=$(read16 "$2")
    if [[ ${4:-} == signed ]] && (( value & 32768 )); then
        printf '%-25s %-4s 0x%04x %7d %s\n' "$1" "$2" "$value" "$((value-65536))" "$3"
    elif [[ $2 == 0x16 ]] && (( value == 65535 )); then
        printf '%-25s %-4s 0xffff   not discharging\n' "$1" "$2"
    else
        printf '%-25s %-4s 0x%04x %7d %s\n' "$1" "$2" "$value" "$value" "$3"
    fi
}
echo "=== sysfs before probe: $(date -Is) ==="
snapshot
# Mark ownership before the write so interruption cannot skip restoration.
unbound=1
printf '%s\n' "$DEV" > "$DRV/unbind"
sleep 0.2
echo '=== identity/control ==='
for query in 'CONTROL_STATUS 0x0000' 'DEVICE_TYPE 0x0001' 'FW_VERSION 0x0002' 'HW_VERSION 0x0003' 'CHEM_ID 0x0008' 'DF_VERSION 0x000c'; do
    read -r label sub <<< "$query"
    value=$(control "$sub")
    printf '%-25s 0x%04x %7d\n' "$label" "$value" "$value"
    if [[ $label == DEVICE_TYPE ]] && (( value != 0x0542 )); then
        echo 'Not a BQ27542-G1; stopping.' >&2
        exit 1
    fi
done
for (( sample=1; sample<=count; sample++ )); do
    echo "=== raw sample $sample/$count: $(date -Is) ==="
    row AtRate 0x02 mA signed
    row UnfilteredSOC 0x04 '%'
    row Temperature 0x06 '0.1 K'
    row Voltage 0x08 mV
    row Flags 0x0a raw
    row NomAvailableCapacity 0x0c mAh
    row FullAvailableCapacity 0x0e mAh
    row RemainingCapacity 0x10 mAh
    row FullChargeCapacity 0x12 mAh
    row AverageCurrent 0x14 mA signed
    row TimeToEmpty 0x16 min
    row FilteredFCC 0x18 mAh
    row SafetyStatus 0x1a raw
    row UnfilteredFCC 0x1c mAh
    row Imax 0x1e mA
    row UnfilteredRM 0x20 mAh
    row FilteredRM 0x22 mAh
    row InternalTemperature 0x28 '0.1 K'
    row CycleCount 0x2a cycles
    row StateOfCharge 0x2c '%'
    row StateOfHealth 0x2e 'raw (low byte = %)'
    row ChargingVoltage 0x30 mV
    row ChargingCurrent 0x32 mA
    row PassedCharge 0x34 mAh signed
    row DOD0 0x36 'raw (0..16384)'
    row SelfDischargeCurrent 0x38 mA signed
    row PackConfiguration 0x3a raw
    row DesignCapacity 0x3c mAh
    if (( sample < count )); then sleep "$interval"; fi
done
if (( dataflash )); then
    python3 "$helper"
fi
printf '%s\n' "$DEV" > "$DRV/bind"
unbound=0
echo "=== sysfs after probe: $(date -Is) ==="
snapshot
