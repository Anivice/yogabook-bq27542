#!/usr/bin/env bash
set -euo pipefail
if [[ $# != 2 || ! $2 =~ ^[0-9]+$ ]] || (( 10#$2 < 1 || 10#$2 > 65535 )); then
    echo 'Usage: bash scripts/stream-discharge.sh RECEIVER_IP PORT' >&2
    exit 2
fi
command -v ncat >/dev/null
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
# Bound-driver sysfs reads only; inhibit idle sleep for the duration.
systemd-inhibit --what=sleep:idle --mode=block --why='Battery discharge measurement' \
    python3 -u discharge_stream.py |
    ncat --send-only --wait 5s --idle-timeout 15s "$1" "$2"
