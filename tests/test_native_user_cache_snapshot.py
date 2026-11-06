"""Independent pure-memory tests of the target-task 16K physical walker.

No capture fixture, debugger, guest, subprocess, or device is used here.
"""
import importlib.util
from pathlib import Path
import struct
import unittest


SOURCE = Path(__file__).resolve().parents[1] / 'scripts/native_user_cache_snapshot.py'
SPEC = importlib.util.spec_from_file_location('native_user_cache_snapshot_walker_test', SOURCE)
reader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reader)


class PhysicalFixture:
    """An independent target root and two noncontiguous resident leaf pages."""
    start = 0x100000
    size = 0x40000
    root = 0x100000
    second = 0x104000
    third = 0x108000
    leaf_a = 0x120000
    leaf_b = 0x128000
    va = 0x18C9A4000

    def __init__(self):
        self.ram = bytearray(self.size)
        self.reads = []
        self.geo = dict(page_shift=14, page_bytes=16384, root_level=1,
                        va_bits=39, pa_bits=40, root_bits=3)
        self.root_site = self.root + ((self.va >> 36) & 7) * 8
        self.second_site = self.second + ((self.va >> 25) & 2047) * 8
        self.third_site = self.third + ((self.va >> 14) & 2047) * 8
        self.word(self.root_site, self.second | 3)
        self.word(self.second_site, self.third | 3)
        self.word(self.third_site, self.leaf_a | 3 | (1 << 6) | (1 << 10))
        self.word(self.third_site + 8, self.leaf_b | 3 | (1 << 6) | (1 << 10))
        self.put(self.leaf_a + 16376, b'ABCDEFGH')
        self.put(self.leaf_b, b'abcdefgh')

    def put(self, address, value):
        self.ram[address - self.start:address - self.start + len(value)] = value

    def word(self, address, value):
        self.put(address, struct.pack('<Q', value))

    def physical(self, address, count):
        self.reads.append((address, count))
        if not self.start <= address <= self.start + self.size - count:
            raise ValueError('test physical read outside actual RAM')
        return bytes(self.ram[address - self.start:address - self.start + count])

    def translate(self, *, executable=False):
        return reader.translate(self.physical, self.va, self.root, self.geo,
                                executable=executable)


class NativeUserCacheWalkerTests(unittest.TestCase):
    def test_two_pages_follow_target_root_and_noncontiguous_leaves(self):
        fixture = PhysicalFixture()
        raw, pages = reader.read_user(fixture.physical, fixture.va + 16376, 16,
                                      fixture.root, fixture.geo, executable=True)
        self.assertEqual(raw, b'ABCDEFGHabcdefgh')
        self.assertEqual([p['bytes'] for p in pages], [8, 8])
        self.assertEqual([int(p['physical_address'], 16) for p in pages],
                         [fixture.leaf_a + 16376, fixture.leaf_b])
        self.assertEqual([p['path'][0]['table'] for p in pages], [hex(fixture.root)] * 2)
        self.assertEqual([p['level'] for p in pages[0]['path']], [1, 2, 3])
        self.assertIn((fixture.third_site + 8, 8), fixture.reads)
        self.assertNotIn((fixture.leaf_a + 16384, 8), fixture.reads)

    def test_table_el0_denial_and_uxn_are_respected(self):
        for permission in (1 << 61, 1 << 60):
            with self.subTest(permission=hex(permission)):
                fixture = PhysicalFixture()
                fixture.word(fixture.root_site, fixture.second | 3 | permission)
                with self.assertRaisesRegex(ValueError, 'table permissions'):
                    fixture.translate(executable=True)
                self.assertEqual(fixture.reads, [(fixture.root_site, 8)])
        fixture = PhysicalFixture()
        fixture.word(fixture.root_site, fixture.second | 3 | (1 << 60))
        self.assertEqual(fixture.translate()['physical_address'], fixture.leaf_a)

    def test_leaf_access_flag_el0_and_execute_permissions(self):
        for attributes in ((1 << 6), (1 << 10), (1 << 6) | (1 << 10) | (1 << 54)):
            with self.subTest(attributes=hex(attributes)):
                fixture = PhysicalFixture()
                fixture.word(fixture.third_site, fixture.leaf_a | 3 | attributes)
                with self.assertRaises(ValueError):
                    fixture.translate(executable=True)
        fixture = PhysicalFixture()
        fixture.word(fixture.third_site, fixture.leaf_a | 3 | (1 << 6) | (1 << 10) | (1 << 54))
        self.assertEqual(fixture.translate()['physical_address'], fixture.leaf_a)

    def test_invalid_or_nonresident_descriptors_are_not_zero_filled(self):
        for descriptor in (0, 2, PhysicalFixture.leaf_a | 1 | (1 << 6) | (1 << 10)):
            with self.subTest(descriptor=hex(descriptor)):
                fixture = PhysicalFixture()
                fixture.word(fixture.third_site, descriptor)
                with self.assertRaises(ValueError):
                    fixture.translate()

    def test_descriptor_pa_overflow_is_rejected_before_masking(self):
        fixture = PhysicalFixture()
        fixture.word(fixture.root_site, fixture.second | 3 | (1 << 40))
        with self.assertRaisesRegex(ValueError, 'physical width'):
            fixture.translate()
        self.assertEqual(fixture.reads, [(fixture.root_site, 8)])

    def test_external_ram_bounds_are_enforced_for_table_and_leaf(self):
        fixture = PhysicalFixture()
        fixture.word(fixture.root_site, 0x200000 | 3)
        with self.assertRaisesRegex(ValueError, 'outside actual RAM'):
            fixture.translate()
        fixture = PhysicalFixture()
        fixture.word(fixture.third_site, 0x200000 | 3 | (1 << 6) | (1 << 10))
        with self.assertRaisesRegex(ValueError, 'outside actual RAM'):
            reader.read_user(fixture.physical, fixture.va, 16, fixture.root, fixture.geo)

    def test_misaligned_root_and_block_are_rejected(self):
        fixture = PhysicalFixture()
        with self.assertRaisesRegex(ValueError, 'root table is misaligned'):
            reader.translate(fixture.physical, fixture.va, fixture.root + 8, fixture.geo)
        self.assertEqual(fixture.reads, [])
        fixture.word(fixture.second_site, fixture.leaf_a | 1 | (1 << 6) | (1 << 10))
        with self.assertRaisesRegex(ValueError, 'output address is misaligned'):
            fixture.translate()

    def test_user_extent_and_count_bounds_precede_physical_reads(self):
        for va, count in ((0, 16), (1 << 39, 16), ((1 << 39) - 8, 16),
                          (PhysicalFixture.va, 0), (PhysicalFixture.va, 513)):
            with self.subTest(va=hex(va), count=count):
                fixture = PhysicalFixture()
                with self.assertRaises(ValueError):
                    reader.read_user(fixture.physical, va, count, fixture.root, fixture.geo)
                self.assertEqual(fixture.reads, [])

    def test_level_zero_block_is_rejected(self):
        fixture = PhysicalFixture()
        geo = dict(fixture.geo, root_level=0, va_bits=48, root_bits=1)
        fixture.word(fixture.root, 1 | (1 << 6) | (1 << 10))
        with self.assertRaisesRegex(ValueError, 'invalid block/page descriptor'):
            reader.translate(fixture.physical, fixture.va, fixture.root, geo)

    def test_geometry_accepts_only_consistent_qualified_16k(self):
        tcr = 25 | (2 << 14) | (2 << 32)
        attr = dict(root=1, max=3, shift=14, size=16384, tcr=tcr,
                    va_mask=(1 << 39) - 1)
        registers = dict(TCR_EL1=hex(tcr), ID_AA64MMFR0_EL1='0x2', SCTLR_EL1='0x1')
        result = reader.geometry(attr, registers)
        self.assertEqual((result['va_bits'], result['pa_bits'], result['root_bits']), (39, 40, 3))
        mutations = [('SCTLR_EL1', '0x0'), ('ID_AA64MMFR0_EL1', '0x1'),
                     ('TCR_EL1', hex(tcr ^ (1 << 14))),
                     ('TCR_EL1', hex(tcr | (1 << 59)))]
        for key, value in mutations:
            with self.subTest(register=key, value=value):
                with self.assertRaises(ValueError):
                    reader.geometry(attr, dict(registers, **{key: value}))


if __name__ == '__main__':
    unittest.main()
