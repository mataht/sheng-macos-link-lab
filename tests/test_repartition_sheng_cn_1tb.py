# SPDX-License-Identifier: GPL-3.0-or-later
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from backup_device import parse_gpt
from create_log_partition import geometry
import repartition_sheng_cn_1tb as cn
from native_log_file import LAYOUT

LBAS = cn.LUN0_LBAS
REAL_DUMP = ROOT / 'private/device/20261106-root-readonly/gpt-primary-sda.bin'


def windows_from_entries(entries, first_usable=6, last_usable=LBAS - 6, disk_guid=uuid.UUID(int=101)):
    """Primary and secondary 1 MiB windows; the backup array sits at disk last - 4 (ptool convention)."""
    copies = []
    for current, other, table, start in ((1, LBAS - 1, 2, 0), (LBAS - 1, 1, LBAS - 5, LBAS - cn.ALIGN)):
        data = bytearray(cn.WINDOW)
        header = bytearray(cn.BLOCK)
        struct.pack_into('<8sIIIIQQQQ16sQIII', header, 0, b'EFI PART', 0x10000, 92, 0, 0, current, other,
                         first_usable, last_usable, disk_guid.bytes_le, table, 32, 128, zlib.crc32(entries))
        struct.pack_into('<I', header, 16, zlib.crc32(header[:92]))
        data[(current - start) * cn.BLOCK:(current - start + 1) * cn.BLOCK] = header
        data[(table - start) * cn.BLOCK:(table - start) * cn.BLOCK + len(entries)] = entries
        copies.append(bytes(data))
    return copies


def synthetic_entries():
    entries = bytearray(32 * 128)
    parts = [(i, f'p{i}', 6 + i * 256, 6 + i * 256 + 255) for i in range(28)] + [(28, 'userdata', 3094592, LBAS - 6)]
    for index, name, first, last in parts:
        at = index * 128
        entries[at:at + 16] = uuid.UUID(int=index + 1).bytes_le
        entries[at + 16:at + 32] = uuid.UUID(int=index + 64).bytes_le
        struct.pack_into('<QQQ', entries, at + 32, first, last, 0x1234)
        entries[at + 56:at + 56 + len(name) * 2] = name.encode('utf-16-le')
    return entries


class RepartitionShengCn(unittest.TestCase):
    def check_plan(self, primary, secondary):
        outputs, plan = cn.layout(primary, secondary, LBAS, uuid.UUID(int=7))
        before = geometry(primary, LBAS, 0)
        after = geometry(outputs['primary'], LBAS, 0)
        backup = geometry(outputs['secondary'], LBAS, LBAS - cn.ALIGN)
        self.assertEqual(after[2], backup[2])
        changed = [i for i in range(32) if before[2][i * 128:(i + 1) * 128] != after[2][i * 128:(i + 1) * 128]]
        self.assertEqual(changed, [28, 29, 30])
        # userdata keeps type, identity, start, attributes and name; only its end moves.
        self.assertEqual(before[2][28 * 128:28 * 128 + 40], after[2][28 * 128:28 * 128 + 40])
        self.assertEqual(before[2][28 * 128 + 48:29 * 128], after[2][28 * 128 + 48:29 * 128])
        self.assertEqual(plan['userdata']['last_lba'] - plan['userdata']['first_lba'] + 1, (128 << 30) // cn.BLOCK)
        self.assertEqual((plan['macoslog']['first_lba'], plan['macoslog']['last_lba']),
                         (LAYOUT['partition_first'], LAYOUT['partition_last']))
        self.assertEqual(plan['macoslog']['index'], LAYOUT['partition_index'])
        log = after[2][29 * 128:30 * 128]
        self.assertEqual(str(uuid.UUID(bytes_le=log[16:32])), LAYOUT['partition_uuid'])
        self.assertEqual(log[56:128].decode('utf-16-le').split('\0')[0], 'macoslog')
        self.assertEqual(plan['macosroot']['last_lba'] - plan['macosroot']['first_lba'] + 1, (32 << 30) // cn.BLOCK)
        self.assertEqual(plan['macosroot']['first_lba'] % cn.ALIGN, 0)
        self.assertEqual(plan['unallocated']['last_lba'], before[0]['last_usable'])
        self.assertGreater(plan['unallocated']['bytes'], 800 << 30)
        self.assertEqual(after[1][24:72], before[1][24:72])
        parse_gpt(outputs['secondary'], cn.BLOCK, LBAS, LBAS - cn.ALIGN)
        return outputs, plan

    def test_synthetic_layout_changes_only_three_entries(self):
        self.check_plan(*windows_from_entries(synthetic_entries()))

    @unittest.skipUnless(REAL_DUMP.exists(), 'requires the local read-only GPT dump of the CN 1 TB tablet')
    def test_actual_tablet_primary_gpt_satisfies_the_plan(self):
        dump = REAL_DUMP.read_bytes()
        header = dump[cn.BLOCK:2 * cn.BLOCK]
        entries_lba, count, stride = struct.unpack_from('<QII', header, 72)
        first, last = struct.unpack_from('<QQ', header, 40)
        self.assertEqual((entries_lba, count, stride), (2, 32, 128))
        entries = dump[2 * cn.BLOCK:2 * cn.BLOCK + count * stride]
        primary, secondary = windows_from_entries(entries, first, last, uuid.UUID(bytes_le=header[56:72]))
        # The primary window keeps the device's real header bytes; only the synthesized backup is fabricated.
        self.assertEqual(primary[cn.BLOCK:cn.BLOCK + 92], header[:92])
        _, plan = self.check_plan(primary, secondary)
        self.assertEqual(plan['userdata']['first_lba'], 3094592)
        self.assertEqual(plan['macosroot']['first_lba'], 36665600)

    def test_refusals(self):
        entries = synthetic_entries()
        cases = {}
        bad = bytearray(entries); struct.pack_into('<Q', bad, 28 * 128 + 40, LBAS - 7)
        cases['userdata not last'] = bad
        bad = bytearray(entries); bad[29 * 128:29 * 128 + 16] = uuid.UUID(int=9).bytes_le
        cases['entry 30 in use'] = bad
        bad = bytearray(entries); bad[28 * 128 + 56:28 * 128 + 72] = 'userdat2'.encode('utf-16-le')
        cases['userdata missing'] = bad
        bad = bytearray(entries); struct.pack_into('<Q', bad, 28 * 128 + 32, 3094592 + 256)
        cases['userdata moved'] = bad
        for label, array in cases.items():
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    cn.layout(*windows_from_entries(array), LBAS)
        primary, secondary = windows_from_entries(entries)
        tampered = bytearray(secondary)
        tampered[(LBAS - 5 - (LBAS - cn.ALIGN)) * cn.BLOCK + 40] ^= 1
        with self.assertRaises(ValueError):
            cn.layout(primary, bytes(tampered), LBAS)
        with self.assertRaises(ValueError):
            cn.layout(primary, secondary, LBAS - 256)
        with patch.dict(cn.LAYOUT, name='sheng-lun4-upstream'):
            with self.assertRaises(ValueError):
                cn.layout(primary, secondary, LBAS)

    def test_apply_writes_backup_then_primary_then_log_and_refuses_stale_tables(self):
        primary, secondary = windows_from_entries(synthetic_entries())
        outputs, plan = cn.layout(primary, secondary, LBAS)

        class Device:
            serial = 'test-serial'
            lbas = LBAS

            def __init__(self):
                self.writes, self.disk = [], {0: primary, LBAS - cn.ALIGN: secondary}

            def require_recovery(self):
                pass

            def read(self, start, count):
                if count == cn.ALIGN and start in self.disk:
                    return self.disk[start]
                raise AssertionError(('unexpected read', start, count))

            def write(self, path, start, count):
                self.writes.append((path.name, start, count))
                if path.name == 'write-secondary-header.bin':
                    self.disk[LBAS - cn.ALIGN] = outputs['secondary']
                if path.name == 'write-primary-header.bin':
                    self.disk[0] = outputs['primary']

        with tempfile.TemporaryDirectory() as work:
            work = Path(work)
            directory = work / 'plan'
            directory.mkdir()
            for name, data in (('before-primary.bin', primary), ('before-secondary.bin', secondary),
                               ('after-primary.bin', outputs['primary']), ('after-secondary.bin', outputs['secondary'])):
                (directory / name).write_bytes(data)
            (directory / 'macoslog-fat16.img').write_bytes(bytes(cn.LOG_BLOCKS * cn.BLOCK))
            from hashlib import sha256
            plan.update(serial='test-serial', before=dict(primary=sha256(primary).hexdigest(),
                                                      secondary=sha256(secondary).hexdigest()))
            (directory / 'plan.json').write_text(json.dumps(plan))
            with patch.object(cn, 'PRESERVED', work / 'preserved'):
                device = Device()
                record = cn.apply(device, directory)
                self.assertTrue(record['applied'])
                self.assertEqual([w[0] for w in device.writes],
                                 ['write-secondary-array.bin', 'write-secondary-header.bin',
                                  'write-primary-array.bin', 'write-primary-header.bin', 'macoslog-fat16.img'])
                self.assertEqual(device.writes[-1][1:], (LAYOUT['partition_first'], cn.LOG_BLOCKS))
                self.assertEqual((work / 'preserved/verified-primary.bin').read_bytes(), outputs['primary'])
                with self.assertRaises(ValueError):
                    cn.apply(device, directory)
            stale = Device()
            stale.disk[0] = outputs['primary']
            plan['applied'] = False
            (directory / 'plan.json').write_text(json.dumps(plan))
            (directory / 'apply.jsonl').unlink()
            with patch.object(cn, 'PRESERVED', work / 'preserved-2'):
                with self.assertRaisesRegex(ValueError, 'changed after preparation'):
                    cn.apply(stale, directory)
            self.assertEqual(stale.writes, [])


if __name__ == '__main__':
    unittest.main()
