# Yoga Book BQ27542 battery reporting module

Initial source package for the Lenovo Yoga Book YB1-X91F. GPL-2.0.

## Changes

Only BQ27542 changes behavior:

* `charge_now` reads RemainingCapacity (0x10), matching the load-compensated
  FullChargeCapacity (0x12), instead of NominalAvailableCapacity (0x0c).
* `state_of_health` exposes the low byte of StateOfHealth (0x2e), in percent.
  Bus errors propagate; percentages above 100 return ENODATA.
* `health` remains the existing fault/status property. Other chips retain
  their existing properties and charge selection.

This fixes reporting, not battery aging or the gauge's learned model. UPower's
existing Capacity/wear field may still differ from the chip's SOH; this package
contains no UPower changes. No reset, unseal, or fuel-gauge configuration commands
were added.

## Source and compatibility

`upstream/` contains unmodified Linux **v7.2** files from:
https://github.com/torvalds/linux/tree/v7.2

Paths: `drivers/power/supply/bq27xxx_battery.c`,
`drivers/power/supply/bq27xxx_battery_i2c.c`, and
`include/linux/power/bq27xxx_battery.h`.
`UPSTREAM.sha256` records their exact bytes.

`src/` contains the patched core, the unchanged I2C implementation, and the
matching shared header. Both C files use a local include for that header.
Both modules are rebuilt together to avoid relying on a different core/I2C
structure layout. Do not load only one of these replacement modules.

Target: Fedora kernel 7.2.4-200.fc44.x86_64. The baseline is upstream v7.2,
not Fedora's exact downstream source RPM. Build against your installed kernel's
matching development tree; compilation and hardware behavior on Fedora remain
to be verified on the laptop. No prebuilt .ko is included.

## Build on Fedora Atomic

Have matching `kernel-devel` installed (not merely `kernel-headers`), plus make,
GCC and ELF development dependencies. On the host, one possible setup is:

```bash
sudo rpm-ostree install kernel-devel gcc make elfutils-libelf-devel
# Reboot if rpm-ostree created a new deployment, then confirm uname -r.
```

The development tree must match the running kernel exactly. Repositories may
no longer carry an old matching kernel-devel; in that case obtain the matching
Fedora RPM or boot the kernel corresponding to the development package. Do not
force a module built for another kernel to load.

```bash
make -j"$(nproc)"
sudo ./scripts/load.sh
cat /sys/class/power_supply/bq27542-*/state_of_health
cat /sys/class/power_supply/bq27542-*/uevent
upower --battery
```

`KDIR=/absolute/path/to/matching/kernel/build` can override the default.
Secure Boot/module-signature enforcement may require signing; the user's current
Yoga Book setup has Secure Boot disabled.

The load script checks vermagic before unloading anything, unloads the I2C client
module then core, and loads the two replacements. It disables the existing
DT-to-NVM update option for the replacement core. It does not unload the
platform module that created the I2C device. Unloading can fail if other consumers
hold references; the script stops rather than forcing removal. Battery reporting
is briefly unavailable during the swap.

Expected: charge_now and charge_full refer to the same compensated capacity
family; capacity remains the chip SOC; state_of_health is its native percentage.
Numbers change with load and time, so compare with near-simultaneous readings.

## Restore / persistence

```bash
sudo ./scripts/restore.sh
```

This reloads Fedora's modules from their normal paths. Reboot also restores the
normal configuration. Neither script installs files into /usr or /lib/modules,
adds blacklists, changes the initramfs, or makes loading persistent.

## Verification and later patch workflow

```bash
python3 scripts/verify.py
bash -n scripts/load.sh scripts/restore.sh
sha256sum -c UPSTREAM.sha256
```

The verifier checks the included upstream patch with `git apply --check`, applies
it in a temporary directory, compares the result to the shipped implementation,
and compiles/runs extracted property branches with mocked reads. It covers
BQ27542 RC selection, unchanged other-chip NAC/RC selection, SOH decoding,
range checking and I2C error propagation. This is not a full kernel module build.

Upload the extracted directory contents to your new GitHub repository. For later
work, provide its URL; changes can be based on the new HEAD and delivered as
patches. Git's verification command is **git apply --check** (`--verify` is not a
git-apply option). `patches/0001-bq27542-charge-now-and-soh.patch` targets an
upstream Linux tree; it is already applied in src/, so do not apply it here.

Specification: TI BQ27542-G1 TRM SLUUB65B, section 15.1.24:
https://www.ti.com/lit/ug/sluub65b/sluub65b.pdf

Initial verification passed: patch application, exact reconstruction, compiled
mock property-branch tests and shell syntax. Full Fedora module compilation and
live battery testing were unavailable in the creation environment.

## Configuration and learned-data dump

```bash
sudo ./scripts/probe.sh --dataflash > /tmp/bq-dataflash.log
```

Requires Python 3 and i2c-tools. This optional mode runs while the battery
client is unbound, then uses the same restoration trap as the ordinary probe.
It reads classes 48 (design/SOH settings), 64 (pack settings), 80 (load/cutoff/
reserve settings), 82 (Qmax/learning state), 88/89 (resistance profiles), and
104 (raw calibration). Both raw bytes and selected decoded values are included.
Offsets are from SLUUB65B Table 16-3. Some narrative examples in that manual
have inconsistent offsets; retain raw data for review. Energy/power fields
are left in raw units until Design Energy Scale is interpreted.

The helper validates BQ27542-G1 firmware 0x0201, requires an unbound bus-0
client, and stops if CONTROL_STATUS says SEALED. It never sends unlock keys.
It writes only identity/status query commands and the temporary data-flash
access/class/block selectors (0x61, 0x3e, 0x3f). It never writes BlockData
(0x40..0x5f), BlockDataChecksum (0x60), reset, IT_ENABLE, or calibration commands.
Selecting a block does not commit configuration; selector state is left at the
last block read. Every block is read twice and compared with its checksum,
with up to three attempts if the gauge updates data during the read.
No coherent whole-flash snapshot is promised while the gauge is operating.

Use external power while collecting this diagnostic. Do not run other raw I2C
tools concurrently. Do not run dataflash.py directly: probe.sh owns the
unbind/rebind lifecycle. The dump does not change the kernel module and needs
no rebuild. Send bq-dataflash.log for analysis before changing stored settings.

## Controlled gauge reset experiment

Connect the charger and leave it connected throughout this experiment:

```sh
sudo ./scripts/probe.sh --reset-gauge > /tmp/bq-reset.log 2>&1
```

This mode changes gauge runtime state: it sends the documented Control RESET
command (0x0041) exactly once, without retries. It is a firmware reset, not a
factory reset or a battery repair. It can change reported capacity and increments
the gauge reset counter. See TI SLUUB65B sections 3.2.1 and 15.1.1.26.

Before sending RESET, it requires the expected unbound bus-0 client, device
0x0542 / firmware 0x0201, an unsealed gauge with Qmax updates enabled (QEN, bit 0), an online
Mains/USB supply, battery voltage at least 4.0 V, and no significant discharge.
It saves checksum-verified diagnostic blocks and raw measurements in a private
`/var/tmp/bq27542-reset-*` directory, flushing the backup to disk first. The
backup covers the same selected classes as `--dataflash`; it is not a complete
flash image or an automatic restore file.

After reset it records raw values at approximately 0, 2, 10 and 30 seconds after
a five-second startup wait, then captures the same diagnostic blocks and reports
changed bytes. It does not write Qmax, Ra, calibration, protection settings,
BlockData or checksums, and does not send unseal keys or IT_ENABLE. The gauge
itself may update stored state during normal operation; the comparison records
those differences without attributing every change to RESET.

CONTROL_STATUS QEN is bit 0; VOK is bit 1. VOK can clear after RESET and
is not an indication that gauging is disabled. After-reset collection does not
require either flag to stay set and records CONTROL_STATUS in each sample.
If an earlier helper stopped with "gauging is disabled" after sending RESET,
collect the current state with `sudo ./scripts/probe.sh --dataflash`;
do not repeat RESET merely to complete the missing readings.

Attach `/tmp/bq-reset.log`. Keep the printed backup directory as well. A reset
failure or timeout is not retried, since the command may already have reached
the device. The usual trap attempts to rebind on errors and catchable signals;
SIGKILL, power loss, or removal of the I2C client cannot be handled by a trap.
Probe instances now share a nonblocking lock. Do not run other raw-I2C tools
concurrently.

The data-flash dump also captures Charge Termination (class 36), Integrity
Data (class 57), and Current Thresholds (class 81). These are diagnostic reads
using the existing block-selector whitelist and checksum verification. It
prints taper current/voltage/window, full-charge flag thresholds, relaxation
thresholds, and the three stored checksum references. A reference of zero may
be unprovisioned; do not rewrite a checksum merely to make its comparison pass.
FC Set % controls flag reporting, while valid charge termination also depends
on taper qualification (TI SLUUB65B section 7.5).

## Model and integrity diagnostics

The ordinary probe now includes RESET_DATA (reset count), the three TI checksum
comparisons, DODatEOC, Qstart, FastQmax and signed AveragePower. Identity and
firmware are checked before extended reads. A 100-ms wait precedes Control
result reads, including the checksum results (TI SLUUB65B section 10.3.2).

```sh
sudo ./scripts/probe.sh --dataflash > /tmp/bq-model.log 2>&1
```

`stored-reference-match=yes` means bit 15 is clear: the computed checksum
matches its stored reference. A mismatch requires investigation; it does not
by itself prove flash corruption, since reference checksums can be stale or
not provisioned. A match does not establish that the chemistry fits this
battery or that learned parameters are accurate. These are firmware checksum
queries, not writes to BlockDataCheckSum (0x60).

AveragePower is signed, in mW for Design Energy Scale 1 or cW for scale 10;
the raw word is printed too. The data-flash decode also includes fast-scaling
limits, activation SOC, load selection and OCV reset temperature threshold.
This mode performs no reset or parameter edits.

## Raw gauge diagnostics

If RC/FCC/SOC are zero, compare raw silicon replies to sysfs before changing
reporting again. A matching raw zero establishes that the zero is not caused by
the CHARGE_NOW register-selection patch; it does not identify the physical or
configuration cause. SOH is a model estimate, not independent proof of cell
capacity. `TimeToEmpty=0xffff` means not discharging, not 65535 minutes remaining.

```bash
sudo ./scripts/probe.sh | tee /tmp/bq27542-probe.log
# Optional: 12 samples, 10 seconds between samples (plus read time)
sudo ./scripts/probe.sh 12 10 | tee /tmp/bq27542-monitor.log
```

Requires i2c-tools. This Yoga Book-specific script validates the bound device,
prints sysfs before/after, temporarily unbinds the I2C battery driver, checks
identity, and reads standard registers plus PackConfiguration and DesignCapacity.
Extra diagnostics include Imax, AtRate, PassedCharge and DOD0. Register values
within a sample are sequential reads, not an atomic snapshot.

Battery reporting is unavailable for the entire probe, especially when taking
multiple samples. The script rebinds on normal exit, read failure, SIGINT and
SIGTERM. SIGKILL/power loss cannot run cleanup; if necessary recover with:

```bash
echo i2c-bq27542 | sudo tee /sys/bus/i2c/drivers/bq27xxx-battery/bind
```

Without `--dataflash`, only whitelisted Control status/identity queries are sent.
In ordinary mode, no reset, unseal, IT_ENABLE, data-flash selection or configuration writes are
performed in that mode. Query
commands and register pointers still require I2C writes as part of reading.
Do not run another raw I2C probe concurrently.

For a charging-zero case, first capture one probe on external power. If normal
charging completes, capture another. A short unplugged comparison can then show
whether compensated FCC changes with charging state; save work first and reconnect
promptly if the machine approaches shutdown. Do not perform a forced deep discharge
or a learning cycle based on these reports alone.

The probe also handles a client left unbound by an earlier interrupted probe:
it resolves `/sys/bus/i2c/devices/i2c-bq27542`, validates its name and bus,
and restores the battery driver before starting. A missing client, unavailable
driver, or client bound to another driver produces a separate error; it does
not create a new client or detach another driver.

## Stream a measured discharge to another machine

This logger uses the bound driver's sysfs voltage/current readings; it never
unbinds the battery or writes gauge/charger registers. It emits JSON Lines once
per second, including cumulative discharged mAh/Wh using trapezoidal integration
of negative current. The design capacity is metadata, not a health estimate.
The initial connected charger Full status is recorded as the full anchor.
No percentage is invented. Current calibration has not been independently verified.

On the receiving Linux machine, copy `scripts/receive_discharge.py`, then run:

```sh
ncat -l --recv-only 5000 | python3 -u receive_discharge.py bq-full-discharge.jsonl
```

The receiver exclusively creates a new file and fsyncs every complete record.
Use a fresh filename each run. It prints live voltage/current/totals. Laptop
power loss does not interrupt disk writes on this machine; the final unreceived
sample or partial record may be lost. A receiver failure or network outage can
still lose data. Use a stable network that does not depend on laptop USB power.
Ensure receiver TCP port 5000 is reachable. This is an unauthenticated stream;
use your trusted LAN/ZeroTier link.

Keep the Yoga Book plugged in while starting. The false 0% reading may trigger
UPower's critical action. For this attended test, temporarily stop and runtime
mask UPower (this does not disable hardware battery protection):

```sh
sudo systemctl mask --runtime --now upower.service
sudo systemd-run --unit=bq-discharge --collect --property=WorkingDirectory="$PWD" \
    /usr/bin/bash scripts/stream-discharge.sh RECEIVER_IP 5000
```

Run from the repository root. Requires python3, ncat (Fedora package nmap-ncat),
and systemd. The transient service survives SSH disconnection. Check its status
with `systemctl status bq-discharge` or logs with `journalctl -u bq-discharge`.
Wait until the receiver displays the start and full_anchor records and live
samples, then unplug. Keep the load reasonably steady and leave the lid open.

The logger ends after three samples at or below 3.250 V, or when the charger is
reconnected. It does not power off the computer. When the receiver prints
`voltage_endpoint`, reconnect immediately. Hardware cutoff may happen first;
in that case reconnect once, and retain the received log. Do not restart the
laptop repeatedly to extract more charge. An earlier stop gives a lower bound
on usable capacity, not a full capacity measurement. Read failures and gaps longer than five
seconds at the default interval are excluded from integration and recorded, so a gapped run cannot be
called a complete measurement. Boundary intervals crossing charger connection
are also excluded. Battery percentage/health do not determine the endpoint.

After reconnecting (or rebooting on the charger), stop the service if necessary
and restore UPower:

```sh
sudo systemctl stop bq-discharge.service
sudo systemctl unmask --runtime upower.service
sudo systemctl start upower.service
```

A reboot also removes the runtime mask. Do not leave UPower masked during
normal use. Share the receiver's JSONL file, together with whether the run
ended at the voltage endpoint, hardware cutoff, or an early/manual stop.
