import hashlib
import importlib.util
import os
from pathlib import Path
import stat
import struct
import sys
import tempfile
import unittest
import uuid
import zlib

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('inspect_macos26_full_image',
                                              SCRIPTS / 'inspect_macos26_full_image.py')
inspector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inspector)
checksum = inspector.checksum

BLOCK = 4096
GROUP = uuid.UUID('11111111-2222-3333-4444-555555555555')
SYSTEM = uuid.UUID('aaaaaaaa-0000-0000-0000-000000000001')
DATA = uuid.UUID('aaaaaaaa-0000-0000-0000-000000000002')


def sealed(block):
    block[:8] = checksum(block)
    return block


def header(block, oid, xid, kind, subtype=0):
    struct.pack_into('<QQII', block, 8, oid, xid, kind, subtype)


def node(address, flags, level, records, *, fixed=None, subtype=0):
    block = bytearray(BLOCK)
    header(block, address, 1, 2 if flags & 1 else 3, subtype)
    if fixed:
        flags |= 4
    stride = 4 if fixed else 8
    struct.pack_into('<HHI', block, 32, flags, level, len(records))
    struct.pack_into('<HH', block, 40, 0, len(records) * stride)
    keys = 56 + len(records) * stride
    end = BLOCK - (40 if flags & 1 else 0)
    key_cursor = value_cursor = 0
    for index, (key, value) in enumerate(records):
        block[keys + key_cursor:keys + key_cursor + len(key)] = key
        value_cursor += len(value)
        block[end - value_cursor:end - value_cursor + len(value)] = value
        if fixed:
            struct.pack_into('<HH', block, 56 + index * stride, key_cursor, value_cursor)
        else:
            struct.pack_into('<HHHH', block, 56 + index * stride, key_cursor, len(key), value_cursor, len(value))
        key_cursor += len(key)
    if flags & 1 and fixed:
        struct.pack_into('<IIII', block, BLOCK - 40, 0, BLOCK, *fixed)
    return sealed(block)


def volume(oid, name, role, identity, *, flags=1, incompat=1, root_to_xid=0, snapshots=0):
    block = bytearray(BLOCK)
    header(block, oid, 3, 13)
    block[32:36] = b'APSB'
    struct.pack_into('<QQQ', block, 40, 0, 0, incompat)
    struct.pack_into('<Q', block, 152, snapshots)
    struct.pack_into('<Q', block, 184, 7)
    block[240:256] = identity.bytes
    struct.pack_into('<Q', block, 264, flags)
    encoded = name.encode()
    block[704:704 + len(encoded)] = encoded
    struct.pack_into('<H', block, 964, role)
    struct.pack_into('<Q', block, 968, root_to_xid)
    block[1008:1024] = GROUP.bytes
    return sealed(block)


def snapshot_record(xid, name, superblock):
    encoded = name.encode() + b'\0'
    key = struct.pack('<Q', (1 << 60) | xid)
    value = struct.pack('<QQQQQIIH', 0, superblock, 1_700_000_000 * 10**9, 0, 0, 0, 0, len(encoded)) + encoded
    return key, value


def container(*, data_flags=1):
    blocks = [bytearray(BLOCK) for _ in range(16)]

    def nx(xid, volumes):
        block = bytearray(BLOCK)
        header(block, 1, xid, 1)
        block[32:36] = b'NXSB'
        struct.pack_into('<IQ', block, 36, BLOCK, 16)
        block[72:88] = uuid.UUID('cccccccc-0000-0000-0000-000000000000').bytes
        struct.pack_into('<IIQ', block, 104, 2, 0, 1)
        struct.pack_into('<Q', block, 160, 3)
        struct.pack_into('<I', block, 180, 100)
        for index, oid in enumerate(volumes):
            struct.pack_into('<Q', block, 184 + 8 * index, oid)
        return sealed(block)

    blocks[0] = nx(1, [])
    blocks[1] = nx(5, [1026, 1027])
    omap = bytearray(BLOCK)
    header(omap, 3, 5, 0xb)
    struct.pack_into('<Q', omap, 48, 4)
    blocks[3] = sealed(omap)
    blocks[4] = node(4, 1, 1, [(struct.pack('<QQ', 1026, 1), struct.pack('<Q', 5))], fixed=(16, 16), subtype=0xb)
    leaf = [(struct.pack('<QQ', 1026, 3), struct.pack('<IIQ', 0, BLOCK, 6)),
            (struct.pack('<QQ', 1026, 7), struct.pack('<IIQ', 0, BLOCK, 15)),
            (struct.pack('<QQ', 1027, 4), struct.pack('<IIQ', 0, BLOCK, 7))]
    blocks[5] = node(5, 2, 0, leaf, fixed=(16, 16), subtype=0xb)
    blocks[6] = volume(1026, 'Macintosh HD', 0x1, SYSTEM, root_to_xid=4, snapshots=8)
    blocks[7] = volume(1027, 'Macintosh HD - Data', 0x40, DATA, flags=data_flags)
    names = [snapshot_record(2, 'com.apple.os.update-ORIGINAL', 9),
             snapshot_record(4, 'com.apple.root.MODIFIED', 10),
             (struct.pack('<Q', (11 << 60) | 0xffffffffffffffff >> 4) + b'\x02\x00x\0', struct.pack('<Q', 4))]
    blocks[8] = node(8, 3, 0, names)
    blocks[9] = volume(1026, 'Macintosh HD', 0x1, SYSTEM, incompat=0x21)
    blocks[10] = volume(1026, 'Macintosh HD', 0x1, SYSTEM)
    return b''.join(bytes(block) for block in blocks)


def gpt_disk(payload, start_lba=40, sectors=256):
    disk = bytearray(sectors * 512)
    disk[start_lba * 512:start_lba * 512 + len(payload)] = payload
    entries = bytearray(128 * 128)
    last = start_lba + len(payload) // 512 - 1
    struct.pack_into('<16s16sQQQ', entries, 0, inspector.APFS_TYPE.bytes_le, uuid.uuid4().bytes_le,
                     start_lba, last, 0)
    name = 'Container'.encode('utf-16le')
    entries[56:56 + len(name)] = name
    head = bytearray(92)
    struct.pack_into('<8sIIIIQQQQ16sQIII', head, 0, b'EFI PART', 0x10000, 92, 0, 0, 1, sectors - 1,
                     34, sectors - 34, uuid.uuid4().bytes_le, 2, 128, 128, zlib.crc32(entries))
    struct.pack_into('<I', head, 16, zlib.crc32(head))
    disk[512:604] = head
    disk[1024:1024 + len(entries)] = entries
    return disk


class InspectFullImageTests(unittest.TestCase):
    def write(self, data):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / 'disk.img'
        path.write_bytes(data)
        os.chmod(path, stat.S_IREAD)
        self.addCleanup(os.chmod, path, stat.S_IREAD | stat.S_IWRITE)
        return path

    def test_reports_volume_group_and_root_snapshot(self):
        report = inspector.inspect(self.write(gpt_disk(container())))
        self.assertEqual(report['layout'], 'gpt')
        [found] = report['containers']
        self.assertEqual(found['checkpoint_xid'], 5)
        system, data = found['volumes']
        self.assertEqual((system['role_name'], data['role_name']), ('System', 'Data'))
        self.assertEqual(system['root_snapshot'], 'com.apple.root.MODIFIED')
        self.assertEqual([item['sealed'] for item in system['snapshots']], [True, False])
        [group] = report['assessment']['system_data_groups']
        self.assertTrue(report['assessment']['single_system_data_group'])
        self.assertEqual(group['volume_group'], str(GROUP).upper())
        self.assertEqual(group['system_volume'], str(SYSTEM).upper())
        self.assertEqual(group['root_snapshot'], 'com.apple.root.MODIFIED')
        self.assertIs(group['root_snapshot_sealed'], False)
        self.assertEqual(group['sealed_snapshots'], ['com.apple.os.update-ORIGINAL'])
        self.assertIs(group['data_encrypted'], False)

    def test_object_map_ignores_mappings_newer_than_the_checkpoint(self):
        report = inspector.inspect(self.write(gpt_disk(container())))
        self.assertEqual(report['containers'][0]['volumes'][0]['name'], 'Macintosh HD')

    def test_encrypted_data_volume_is_reported(self):
        report = inspector.inspect(self.write(gpt_disk(container(data_flags=0))))
        self.assertIs(report['assessment']['system_data_groups'][0]['data_encrypted'], True)

    def test_bare_container(self):
        report = inspector.inspect(self.write(container()))
        self.assertEqual(report['layout'], 'bare-container')
        self.assertEqual(len(report['containers'][0]['volumes']), 2)

    def test_partition_hash(self):
        payload = container()
        report = inspector.inspect(self.write(gpt_disk(payload)), hash_partitions=True)
        self.assertEqual(report['gpt']['partitions'][0]['sha256'], hashlib.sha256(payload).hexdigest())

    def test_rejects_corrupt_gpt_entries(self):
        disk = gpt_disk(container())
        disk[1024 + 60] ^= 1
        with self.assertRaisesRegex(ValueError, 'entry array CRC'):
            inspector.inspect(self.write(disk))

    def test_rejects_corrupt_volume_superblock(self):
        payload = bytearray(container())
        payload[7 * BLOCK + 300] ^= 1
        with self.assertRaisesRegex(ValueError, 'invalid volume superblock'):
            inspector.inspect(self.write(gpt_disk(bytes(payload))))

    def test_rejects_container_larger_than_partition(self):
        payload = bytearray(container())
        struct.pack_into('<Q', payload, BLOCK + 40, 64)
        payload[BLOCK:BLOCK + 8] = checksum(payload[BLOCK:2 * BLOCK])
        struct.pack_into('<Q', payload, 40, 64)
        payload[:8] = checksum(payload[:BLOCK])
        with self.assertRaisesRegex(ValueError, 'beyond its partition'):
            inspector.inspect(self.write(gpt_disk(bytes(payload))))


if __name__ == '__main__':
    unittest.main()
