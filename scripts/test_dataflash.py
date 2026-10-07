#!/usr/bin/env python3
import contextlib
import io
import unittest
from unittest.mock import patch
import dataflash as m

class FakeGauge(m.Gauge):
    def __init__(self, bad=False):
        self.calls=[]
        self.bad=bad
        self.cls=48
        self.index=0
    def transfer(self,*args):
        self.calls.append(args)
        if args[0]=='w2@0x55':
            reg,val=int(args[1],16),int(args[2],16)
            if reg==0x3e:self.cls=val
            if reg==0x3f:self.index=val
            return ''
        reg,length=int(args[1],16),int(args[2][1:])
        data=bytes((self.cls+self.index+i)&255 for i in range(32))
        if reg==0x40: out=data
        elif reg==0x60:out=bytes([(255-(sum(data)&255)) ^ int(self.bad)])
        else:raise AssertionError('unexpected read')
        assert len(out)==length
        return ' '.join(f'0x{x:02x}' for x in out)

class Client:
    def __init__(self,kind='client'):self.kind=kind
    def exists(self):return self.kind!='driver'
    def __truediv__(self,name):return Client(name)
    def read_text(self):return 'bq27542\n'
    def resolve(self):return self
    @property
    def parent(self):return type('Parent',(),{'name':'i2c-0'})()

class Tests(unittest.TestCase):
    def test_block_and_write_allowlist(self):
        g=FakeGauge()
        with patch.object(m.time,'sleep'):
            b,c=g.block(80,2)
        self.assertEqual(b,bytes(range(82,114)))
        self.assertEqual(c,255-(sum(b)&255))
        writes=[a for a in g.calls if a[0]=='w2@0x55']
        self.assertEqual(writes,[('w2@0x55','0x61','0x0'),('w2@0x55','0x3e','0x50'),('w2@0x55','0x3f','0x2')])
        for reg in (0x40,0x60,0x00):
            with self.assertRaises(ValueError):g.select(reg,0)
        with self.assertRaises(ValueError):g.block(112,0)
        with self.assertRaises(ValueError):g.control_query(0x41)
    def test_checksum_failure(self):
        g=FakeGauge(bad=True)
        with patch.object(m.time,'sleep'),self.assertRaises(RuntimeError):g.block(48,0)
        self.assertEqual(sum(a[0]=='w2@0x55' for a in g.calls),9)
    def test_decode_big_endian_signed(self):
        data={cls:bytearray(32*len(blocks)) for cls,blocks in m.BLOCKS.items()}
        data[36][0:2]=(100).to_bytes(2,'big')
        data[36][9]=255
        data[57][8:10]=(0x7e80).to_bytes(2,'big')
        data[82][0:2]=(4163).to_bytes(2,'big')
        data[48][16:18]=(-400).to_bytes(2,'big',signed=True)
        data[80][64:66]=(3000).to_bytes(2,'big')
        out=io.StringIO()
        with contextlib.redirect_stdout(out):m.decode(data)
        lines=out.getvalue().splitlines()
        self.assertTrue(any('Taper Current' in l and '100' in l for l in lines))
        self.assertTrue(any('FC Set %' in l and '-1' in l for l in lines))
        self.assertTrue(any('Chem DF Checksum reference' in l and '32384' in l for l in lines))
        self.assertTrue(any('Qmax Cell 0' in l and '4163' in l for l in lines))
        self.assertTrue(any('SOH Load I' in l and '-400' in l for l in lines))
        self.assertTrue(any('Terminate Voltage' in l and '3000' in l for l in lines))
    def test_sealed_stops_before_selection(self):
        g=FakeGauge()
        g.control_query=lambda q:{1:0x0542,2:0x0201,0:0x601b}[q]
        with patch.object(m,'Path',return_value=Client()),patch.object(m.os,'geteuid',return_value=0),patch.object(m,'Gauge',return_value=g),contextlib.redirect_stdout(io.StringIO()),self.assertRaisesRegex(RuntimeError,'SEALED'):
            m.main()
        self.assertEqual(g.calls,[])

if __name__=='__main__':unittest.main()
