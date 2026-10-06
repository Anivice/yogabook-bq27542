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

Only whitelisted Control status/identity queries are sent. No reset, unseal,
IT_ENABLE, data-flash selection or configuration writes are performed. Query
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
