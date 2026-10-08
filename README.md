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

This reloads Fedora's modules from their normal paths. Without the optional
services below, reboot also restores the normal configuration. Neither manual
script installs files into /usr or /lib/modules, adds blacklists, changes the
initramfs, or makes loading persistent.

## Persistent module loading and consumption recording

Build for the running kernel, then install and start the boot services:

```sh
make -j4
sudo ./scripts/install-services.sh
systemctl status yogabook-bq27542-load.service yogabook-bq27542-record.service
sudo tail -n 10 /var/log/battery.log
```

The installer copies both modules and the existing load helper to
`/var/lib/yogabook-bq27542/modules/$(uname -r)/`, installs the recorder under
`/var/lib/yogabook-bq27542/bin/`, and installs service units under
`/etc/systemd/system/`. This works with Fedora Atomic's writable `/var` and
`/etc`. It preserves existing measurements and configuration on reinstall.
After a kernel update, rebuild and rerun the installer for that kernel. If its
module pair is missing or incompatible, loading fails and recording does not
start. No files are installed into `/usr` or `/lib/modules`.

`yogabook-bq27542-load.service` loads the replacement module pair at boot.
`yogabook-bq27542-record.service` starts afterwards and survives SSH disconnects.
It reads the bound driver's `voltage_now` and `current_now` every second, using
trapezoidal integration of discharge power, and appends approximately every
30 seconds. Negative battery current counts as consumption; positive current
and zero current contribute zero. Charging time is recorded with zero usage.
It does not query raw I2C, unbind the driver, write gauge settings, or change
reported percentage or health. The optional estimator below consumes its
journal separately.

The authoritative history is `/var/log/battery.log`, a plain-text append log:

```text
# battery-log-v1: epoch_end_s duration_s energy_used_uWh
1791446400 30 33333
```

Each data row has exactly three unsigned integer fields: the interval's end
timestamp (UNIX epoch seconds), its measured duration (seconds, rounded to the
nearest second, with a minimum of one for a partial interval), and discharged
energy (micro-watt-hours). For example, 4 V at 1 A discharge for 30 seconds
produces approximately 33333 micro-watt-hours. Sub-unit energy is carried into
later rows within a recorder run. This is measured-current integration, with
accuracy limited by the gauge readings and sample interval; no capacity,
health, percentage, filtering, decay, or remaining-life estimate is produced by
the recorder itself. Charging energy, end voltage/current, and charger status
are now included in preceding `# interval-v2` comments. The three numeric
columns retain their original meaning and existing three-column readers work.
`# state` comments record charger transitions; pending intervals are flushed
before a transition is recorded. `# session` marks each recorder restart.

```text
# interval-v2 1791446430 30 25000 4200000 700000 Charging
1791446430 30 0
```

This means 25000 micro-watt-hours entered the battery and zero were discharged
in the interval. Metadata fields are epoch end, duration, energy entering the
battery (uWh), end voltage (uV), end current (uA), and charger status. Status
spaces are replaced with underscores (`Not_charging`). Voltage/current are
end samples, while both energy values are integrated across the interval.

The recorder keeps only the active interval in memory. Downstream readers
should accept only complete rows containing exactly three unsigned integers
and skip comments/malformed rows. File order is append order. Epoch timestamps
can repeat or move backwards when the wall clock changes; do not discard rows
by blindly putting them into a map with unique timestamp keys. Elapsed duration
uses a separate clock that includes suspend, so wall-clock corrections do not
change the integrated energy.

Missing readings and sampling gaps longer than five seconds (or three sample
intervals if larger) end the current partial record and add a `# gap` comment.
No energy is invented across these gaps, suspend, or service downtime. A clean
service stop saves the final partial interval. Each append is fsynced, but
sudden power loss can still lose the current unsaved interval or damage storage;
fsync is not a guarantee against hardware failure. An incomplete trailing row
is marked invalid when recording resumes. Rename-and-create rotation is
supported; retain rotated files as part of the downstream history rather than
discarding them. No automatic rotation or retention policy is installed.

Change intervals without changing the file format:

```sh
sudoedit /etc/yogabook-bq27542-recorder.conf
sudo systemctl restart yogabook-bq27542-record.service
```

Defaults are `BQ_RECORD_INTERVAL_SECONDS=30` and
`BQ_SAMPLE_INTERVAL_SECONDS=1`. The record interval accepts 1..86400 seconds;
the sample interval accepts 0.1..5 seconds and must not exceed the record
interval. Recording works while connected or disconnected from the charger.
To inspect failures, use `journalctl -u yogabook-bq27542-record.service`.

To disable persistence and restore Fedora's modules:

```sh
sudo systemctl disable --now yogabook-bq27542-estimate.service yogabook-bq27542-record.service yogabook-bq27542-load.service
sudo ./scripts/restore.sh
```

Stopping the loading service alone leaves the loaded modules in place. Existing
logs and configuration are retained. Recorder regression tests can be run with
`python3 -m unittest discover -s scripts -p 'test_record_battery.py'`.

## Full and remaining usable-energy estimator

`scripts/install-services.sh` also installs and enables
`yogabook-bq27542-estimate.service`. No kernel-source changes are needed for
this stage; reuse the module pair built for the running kernel. Apply the patch
and rerun the installer, then inspect:

```sh
sudo ./scripts/install-services.sh
systemctl status yogabook-bq27542-estimate.service
cat /tmp/bat_full /tmp/bat_cur
journalctl -u yogabook-bq27542-estimate.service -n 10
```

Both files contain one integer and a newline, in **micro-watt-hours**:
`bat_full` is estimated full usable energy; `bat_cur` is estimated remaining
usable energy, clamped to 0..full. Divide by 1000000 for Wh. Estimated percentage
is `100 * bat_cur / bat_full`. Each output is replaced atomically and is visible
in the host `/tmp`; the service does not use a private temporary directory.
These are estimates, not native gauge capacity, cell health, or kernel/UPower
percentage overrides. They survive SSH disconnection and are recreated at boot.

The service reads `/sys/class/power_supply/bq25890-charger-0/status` once per
second and opens the journal every ten seconds, processing new complete lines
in append order. Charger-state changes trigger an additional immediate refresh.
The recorder saves at its configured interval (30 seconds by default), so normal
estimates can lag measured consumption by up to that interval plus the refresh
delay. For finer updates, set `BQ_RECORD_INTERVAL_SECONDS=10` in the recorder
configuration and restart that service; the log format is unchanged.

Battery-terminal voltage times signed battery current supplies net energy.
Negative current subtracts energy; positive current adds energy multiplied by
the estimated charge-to-usable-energy efficiency. Laptop workload reduces
measured positive battery current (or makes it negative), so adapter wattage,
CPU/GPU utilization, backlight, and Halo keyboard brightness are not needed as
proxy inputs. Battery voltage also identifies the configured low endpoint.
Current/voltage calibration and finite sampling still limit accuracy.

The initial full estimate is **8989091 uWh (8.989091 Wh)**: the total observed
discharge in the supplied October 8 journal. That old journal contains no
charging energy or charger anchors, and includes an 81-minute gap. Its total
is a provisional seed, not a verified full-cycle capacity measurement; old zero
rows cannot reconstruct charge or identify charge completion. Old rows are
read without inventing charged energy. If startup has neither saved remaining
energy nor a Full anchor, a simple voltage-based fraction supplies an explicitly
uncertain initial estimate. It is not repeatedly applied while discharging.

`Full` anchors remaining energy at 100%. Other charger states, including
`Not charging`, never imply full. An interrupted charge retains the measured
partial refill, saves it, and resumes subtracting discharge, without changing
the full-capacity estimate or claiming a completed calibration.

The usable low endpoint defaults to **3.250 V under load**, following the
earlier attended discharge test. It is an estimation boundary, not proof of
physical cell exhaustion or an instruction to discharge to hardware cutoff.
On a recorded negative-current interval at/below that endpoint, remaining energy
is anchored at zero. An early voltage sag after a Full anchor is ignored until
at least a quarter of the prior full estimate has been delivered. Full-to-low
measured net delivery becomes a capacity candidate; when charging next reaches
Full, valid low-to-Full measurements update full capacity with 20% new-cycle
weight and 80% previous estimate. Capacity learning uses the discharge candidate
when available; otherwise it uses efficiency-adjusted refill from the low anchor.
Thus usable capacity reflects the configured endpoint and workload, not the
8680 mAh design rating. Voltage sag and charge losses can still bias it.

Charge efficiency initially assumes **0.9**. Valid Full-to-Full round trips
calibrate it from discharge/charge energy; a Full-to-low-to-Full cycle can use
its measured discharge candidate. Efficiency updates also use 20% new-cycle
weight. Ratios outside 0.5..1 are rejected, as are grossly implausible capacity
candidates. Incomplete/suspended/restarted/rotated or legacy-only cycles do not
teach capacity or efficiency. Gaps retain an uncertain remaining estimate until
a new anchor; no sleep/offline consumption is fabricated. A new low or Full
anchor can begin a fresh cycle after an earlier gap.

The derived checkpoint `/var/lib/yogabook-bq27542/estimate-state.json` saves
learned full/current energy, efficiency, cycle accumulators, and journal byte
position after each refresh. The journal remains the measurement authority;
checkpointing avoids rereading or subtracting already processed rows on service
restart. The JSON `uncertain` flag and journal messages show estimate quality.
Complete log rows are consumed once; incomplete trailing writes are retried.
Rename-and-create rotation or truncation marks the current cycle uncertain.
Keep historical logs; do not replace the active file with a copy of old history
while retaining its checkpoint, which would replay that history.

Configuration is `/etc/yogabook-bq27542-estimator.conf`:

```text
BQ_INITIAL_FULL_UWH=8989091
BQ_CHARGE_EFFICIENCY=0.9
BQ_EMPTY_UV=3250000
```

Seed and efficiency initialize new checkpoints only; learned values survive
restarts. Changing the voltage endpoint invalidates the pending cycle. Restart
with `sudo systemctl restart yogabook-bq27542-estimate.service`. To disable all
boot services, include the estimator alongside the record/load services in the
disable command. All regression tests run with:

```sh
python3 -m unittest discover -s scripts -p 'test_*.py'
```

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

## Compare constant-current and constant-power capacity models

With the cable connected, `--load-mode-experiment` tests Load Mode 1 (constant
power) against Load Mode 0 (constant current). Load Select stays at 1. This is
an explicit data-flash write experiment, not a confirmed capacity repair.
The helper requires the expected unbound bus-0 BQ27542-G1 firmware 0x0201,
unsealed state, an online BQ25890 charger, suitable voltage/temperature, and
QEN enabled. It does not unseal the gauge.

Before any configuration write it exclusively creates and fsyncs
`/var/lib/yogabook-bq27542/load-mode-backup.json`, including the original
32-byte IT Cfg block and selected diagnostic blocks. An existing backup
prevents another experiment. Only subclass 80 offset 1 and its checksum are
written. The entire resulting block is read back and compared, then one RESET
activates the configuration. CONTROL_STATUS.LDMD must match the chosen mode.
Five raw samples over approximately one minute capture FCC, RM, SOC, SOH,
voltage/current and flags. Load Mode 1 is then restored and verified, with a
second RESET. Firmware resets and normal learning can change runtime/model
state; the backup is diagnostic evidence, not an automatic rollback of all
learned data. Charging limits, chemistry, calibration, Qmax and resistance
configuration are not written by the helper.

Apply the patch, then run from the repository root while plugged in:

```sh
sudo systemd-run --unit=bq-load-mode --collect --property=WorkingDirectory="$PWD" \
    --property=StandardOutput=append:/var/tmp/bq-load-mode.log \
    --property=StandardError=append:/var/tmp/bq-load-mode.log \
    /usr/bin/bash scripts/probe.sh --load-mode-experiment
```

The transient service survives SSH disconnection. Keep the cable connected
until it finishes, normally within two minutes. Check with
`systemctl status bq-load-mode.service`; inspect `/var/tmp/bq-load-mode.log`.
A successful run reports both `original_mode_restored` and
`experiment_complete`. Share that log. The kernel driver is unavailable
throughout the probe and is rebound afterwards. Ordinary probe/data-flash
modes still do not commit configuration writes.

Exceptions, SIGINT, SIGTERM and SIGHUP after the first write attempt trigger
one automatic restoration attempt; the shell waits for that attempt before
rebinding. SIGKILL, power loss, an unplugged charger, or I2C failure can prevent
restoration. Load Mode writes persist across reboot. After such a failure,
connect the cable and recover using the saved backup:

```sh
sudo ./scripts/probe.sh --restore-load-mode \
    /var/lib/yogabook-bq27542/load-mode-backup.json
```

Recovery verifies identity, backup integrity and all other bytes of the IT Cfg
block before writing only the original mode and checksum. It does not blindly
overwrite the entire backup. Keep the backup; do not automatically retry the
experiment or delete it just to bypass the existing-backup guard.
