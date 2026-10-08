#!/usr/bin/env bash
# Install an already-built module pair plus the two measurement services.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
[[ $EUID == 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
command -v python3 >/dev/null
command -v systemctl >/dev/null
for name in bq27xxx_battery bq27xxx_battery_i2c; do
    [[ -f src/$name.ko ]] || { echo "Build first: missing src/$name.ko" >&2; exit 1; }
    [[ $(modinfo -F vermagic "src/$name.ko") == "$(uname -r) "* ]] || {
        echo 'Kernel version mismatch; rebuild before installing services.' >&2; exit 1;
    }
done
# Preserve an existing log; refuse symlinks and non-regular files.
if [[ -L /var/log/battery.log || ( -e /var/log/battery.log && ! -f /var/log/battery.log ) ]]; then
    echo '/var/log/battery.log must be a regular file.' >&2; exit 1
fi
base=/var/lib/yogabook-bq27542
runtime=$base/modules/$(uname -r)
# All prerequisites were checked before stopping the previous recorder.
if [[ $(systemctl show -p LoadState --value yogabook-bq27542-record.service) != not-found ]]; then
    systemctl stop yogabook-bq27542-record.service
fi
install -d -m 0755 "$base/bin" "$runtime/src" "$runtime/scripts"
install -m 0644 src/bq27xxx_battery.ko src/bq27xxx_battery_i2c.ko "$runtime/src/"
install -m 0755 scripts/load.sh "$runtime/scripts/"
install -m 0755 scripts/load-installed.sh scripts/record_battery.py "$base/bin/"
install -m 0644 systemd/yogabook-bq27542-load.service systemd/yogabook-bq27542-record.service /etc/systemd/system/
if [[ ! -e /etc/yogabook-bq27542-recorder.conf ]]; then
    install -m 0644 config/recorder.conf /etc/yogabook-bq27542-recorder.conf
fi
if [[ ! -e /var/log/battery.log ]]; then
    (set -o noclobber; : > /var/log/battery.log)
    chmod 0644 /var/log/battery.log
fi
if command -v restorecon >/dev/null; then
    restorecon -RF "$base" /etc/systemd/system/yogabook-bq27542-{load,record}.service /etc/yogabook-bq27542-recorder.conf /var/log/battery.log
fi
systemctl daemon-reload
systemctl enable yogabook-bq27542-load.service yogabook-bq27542-record.service
systemctl restart yogabook-bq27542-load.service
systemctl start yogabook-bq27542-record.service
echo 'Services installed. Consumption log: /var/log/battery.log'
