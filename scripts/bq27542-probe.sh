#!/usr/bin/env bash
set -euo pipefail

BUS=0
ADDR=0x55
DEV=i2c-bq27542
DRV=/sys/bus/i2c/drivers/bq27xxx-battery

rebind()
{
    if [ ! -L "$DRV/$DEV" ]; then
        echo
        echo "=== rebinding kernel driver ==="
        echo "$DEV" > "$DRV/bind" || true
    fi
}

trap rebind EXIT INT TERM

read16()
{
    local reg="$1"
    local lo hi value

    read -r lo hi < <(
        i2ctransfer -y "$BUS" "w1@$ADDR" "$reg" r2
    )

    value=$(( lo | (hi << 8) ))
    printf '0x%04x %5u' "$value" "$value"
}

control()
{
    local sub="$1"
    local lo hi

    lo=$(( sub & 255 ))
    hi=$(( (sub >> 8) & 255 ))

    # These are query subcommands only.
    i2ctransfer -y "$BUS" \
        "w3@$ADDR" \
        0x00 \
        "$(printf '0x%02x' "$lo")" \
        "$(printf '0x%02x' "$hi")" >/dev/null

    sleep 0.05
    read16 0x00
}

signed16()
{
    local x="$1"

    if (( x & 0x8000 )); then
        printf '%d' "$((x - 65536))"
    else
        printf '%d' "$x"
    fi
}

read16_value()
{
    local reg="$1"
    local lo hi

    read -r lo hi < <(
        i2ctransfer -y "$BUS" "w1@$ADDR" "$reg" r2
    )

    echo $(( lo | (hi << 8) ))
}

echo "=== driver before probe ==="
ls -l "$DRV/$DEV"

echo
echo "=== unbinding bq27xxx-battery ==="
echo "$DEV" > "$DRV/unbind"

sleep 0.2

echo
echo "=== identity / control queries ==="
printf '%-24s ' 'CONTROL_STATUS'
control 0x0000
echo

printf '%-24s ' 'DEVICE_TYPE'
control 0x0001
echo '   (BQ27542-G1 expected: 0x0542)'

printf '%-24s ' 'FW_VERSION'
control 0x0002
echo '   (BQ27542-G1 expected: 0x0201)'

printf '%-24s ' 'HW_VERSION'
control 0x0003
echo

printf '%-24s ' 'CHEM_ID'
control 0x0008
echo

printf '%-24s ' 'DF_VERSION'
control 0x000c
echo

echo
echo "=== standard DataRAM ==="

printf '%-28s ' 'UnfilteredSOC (0x04)'
read16 0x04
echo ' %'

printf '%-28s ' 'Temperature (0x06)'
read16 0x06
echo ' 0.1 K'

printf '%-28s ' 'Voltage (0x08)'
read16 0x08
echo ' mV'

printf '%-28s ' 'Flags (0x0a)'
read16 0x0a
echo

printf '%-28s ' 'NomAvailableCapacity (0x0c)'
read16 0x0c
echo ' mAh'

printf '%-28s ' 'FullAvailableCapacity (0x0e)'
read16 0x0e
echo ' mAh'

printf '%-28s ' 'RemainingCapacity (0x10)'
read16 0x10
echo ' mAh'

printf '%-28s ' 'FullChargeCapacity (0x12)'
read16 0x12
echo ' mAh'

v=$(read16_value 0x14)
printf '%-28s 0x%04x %5d mA\n' \
    'AverageCurrent (0x14)' "$v" "$(signed16 "$v")"

printf '%-28s ' 'TimeToEmpty (0x16)'
read16 0x16
echo ' min'

printf '%-28s ' 'FCC Filtered (0x18)'
read16 0x18
echo ' mAh'

printf '%-28s ' 'SafetyStatus (0x1a)'
read16 0x1a
echo

printf '%-28s ' 'FCC Unfiltered (0x1c)'
read16 0x1c
echo ' mAh'

printf '%-28s ' 'RM Unfiltered (0x20)'
read16 0x20
echo ' mAh'

printf '%-28s ' 'RM Filtered (0x22)'
read16 0x22
echo ' mAh'

printf '%-28s ' 'InternalTemperature (0x28)'
read16 0x28
echo ' 0.1 K'

printf '%-28s ' 'CycleCount (0x2a)'
read16 0x2a
echo

printf '%-28s ' 'StateOfCharge (0x2c)'
read16 0x2c
echo ' %'

printf '%-28s ' 'StateOfHealth (0x2e)'
read16 0x2e
echo ' raw'

printf '%-28s ' 'ChargingVoltage (0x30)'
read16 0x30
echo ' mV'

printf '%-28s ' 'ChargingCurrent (0x32)'
read16 0x32
echo ' mA'

echo
echo "Probe complete."
