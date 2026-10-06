#!/usr/bin/env python3
"""Verify patch reconstruction and execute the modified property branches."""
from pathlib import Path
import subprocess, tempfile
root = Path(__file__).resolve().parents[1]
source = (root/'src/bq27xxx_battery.c').read_text()
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    target = tmp/'drivers/power/supply/bq27xxx_battery.c'
    target.parent.mkdir(parents=True)
    target.write_bytes((root/'upstream/bq27xxx_battery.c').read_bytes())
    patch = str(root/'patches/0001-bq27542-charge-now-and-soh.patch')
    subprocess.run(['git','apply','--check',patch],cwd=tmp,check=True)
    subprocess.run(['git','apply',patch],cwd=tmp,check=True)
    expected = source.replace('#include "bq27xxx_battery.h"','#include <linux/power/bq27xxx_battery.h>')
    assert target.read_text() == expected
    charge = source.split('\tcase POWER_SUPPLY_PROP_CHARGE_NOW:',1)[1].split('\tcase POWER_SUPPLY_PROP_CHARGE_FULL:',1)[0]
    soh = source.split('\tcase POWER_SUPPLY_PROP_STATE_OF_HEALTH:',1)[1].split('\tcase POWER_SUPPLY_PROP_HEALTH:',1)[0]
    harness = '''#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#define BQ27542 18
#define BQ27XXX_REG_NAC 0
#define INVALID_REG_ADDR 255
union power_supply_propval { int intval; };
struct bq27xxx_device_info { int chip; unsigned char regs[1]; struct { int (*read)(struct bq27xxx_device_info*, unsigned char, bool); } bus; };
static int raw;
static int read_mock(struct bq27xxx_device_info*d,unsigned char reg,bool single) { (void)d; assert(reg==0x2e); assert(!single); return raw; }
static int bq27xxx_battery_read_rc(struct bq27xxx_device_info*d,union power_supply_propval*v) { (void)d;v->intval=350000;return 0; }
static int bq27xxx_battery_read_nac(struct bq27xxx_device_info*d,union power_supply_propval*v) { (void)d;v->intval=6078000;return 0; }
static int charge(struct bq27xxx_device_info*di,union power_supply_propval*val) { int ret=0; switch(0) { case 0: CHARGE } return ret; }
static int soh(struct bq27xxx_device_info*di,union power_supply_propval*val) { int ret=0; switch(0) { case 0: SOH } return ret; }
int main(void) {
 struct bq27xxx_device_info d={.chip=BQ27542,.regs={12},.bus={read_mock}}; union power_supply_propval v;
 assert(charge(&d,&v)==0 && v.intval==350000);
 d.chip=17; assert(charge(&d,&v)==0 && v.intval==6078000);
 d.regs[0]=255; assert(charge(&d,&v)==0 && v.intval==350000);
 assert(soh(&d,&v)==-EINVAL); d.chip=BQ27542;
 raw=0x0024; assert(soh(&d,&v)==0 && v.intval==36);
 raw=0xab64; assert(soh(&d,&v)==0 && v.intval==100);
 raw=0; assert(soh(&d,&v)==0 && v.intval==0);
 raw=101; assert(soh(&d,&v)==-ENODATA);
 raw=-EIO; assert(soh(&d,&v)==-EIO);
 return 0;
}
'''.replace('CHARGE',charge).replace('SOH',soh)
    (tmp/'test.c').write_text(harness)
    subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror',str(tmp/'test.c'),'-o',str(tmp/'test')],check=True)
    subprocess.run([str(tmp/'test')],check=True)
print('PASS: git apply --check, exact patch reconstruction, compiled property-branch tests')
