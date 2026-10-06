ifneq ($(KERNELRELEASE),)
obj-m += src/bq27xxx_battery.o
obj-m += src/bq27xxx_battery_i2c.o
else
KDIR ?= /lib/modules/$(shell uname -r)/build
.PHONY: all clean
all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules
clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean
endif
