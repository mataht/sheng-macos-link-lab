# SPDX-License-Identifier: GPL-3.0-or-later
import importlib.util
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from create_log_partition import geometry  # noqa: E402
import repartition_macos26_full as full  # noqa: E402
import repartition_sheng_cn_1tb as cn  # noqa: E402

spec = importlib.util.spec_from_file_location('cn_fixture', ROOT / 'tests/test_repartition_sheng_cn_1tb.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
LBAS = cn.LUN0_LBAS
GIB = 1 << 30


def step4_copies():
    primary, secondary = fixture.windows_from_entries(fixture.synthetic_entries())
    outputs, _ = cn.layout(primary, secondary, LBAS, uuid.UUID(int=7))
    return outputs['primary'], outputs['secondary']


class FakeDevice:
    serial = 'test-serial'
    lbas = LBAS

    def __init__(self, primary, secondary):
        self.blocks = {}
        for data, start in ((primary, 0), (secondary, LBAS - cn.ALIGN)):
            for i in range(cn.ALIGN):
                self.blocks[start + i] = data[i * cn.BLOCK:(i + 1) * cn.BLOCK]
        self.writes = []

    def require_recovery(self):
        pass

    def read(self, start, count):
        return b''.join(self.blocks.get(start + i, bytes(cn.BLOCK)) for i in range(count))

    def write(self, path, start, count):
        data = path.read_bytes()
        assert len(data) == count * cn.BLOCK
        self.writes.append((path.name, start, count))
        for i in range(count):
            self.blocks[start + i] = data[i * cn.BLOCK:(i + 1) * cn.BLOCK]


class RepartitionFullTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)
        self.primary, self.secondary = step4_copies()
        self.step4 = self.dir / 'step4'
        self.step4.mkdir()
        (self.step4 / 'verified-primary.bin').write_bytes(self.primary)
        (self.step4 / 'verified-secondary.bin').write_bytes(self.secondary)

    def test_only_entry_32_changes_and_follows_macosroot(self):
        outputs, plan = full.layout_full(self.primary, self.secondary, LBAS, 128 * GIB, uuid.UUID(int=9))
        before = geometry(self.primary, LBAS, 0)
        root = next(p for p in before[0]['partitions'] if p['name'] == 'macosroot')
        self.assertEqual(plan['full_first_lba'], cn.up(root['last_lba'] + 1))
        self.assertEqual(plan['full_last_lba'] - plan['full_first_lba'] + 1, 128 * GIB // cn.BLOCK)
        self.assertEqual((plan['full_index'], plan['full_name']), (32, 'macossystem'))
        for name, start in (('primary', 0), ('secondary', LBAS - cn.ALIGN)):
            after = geometry(outputs[name], LBAS, start)
            old = geometry(self.primary if name == 'primary' else self.secondary, LBAS, start)
            for i in range(32):
                same = after[2][i * 128:(i + 1) * 128] == old[2][i * 128:(i + 1) * 128]
                self.assertEqual(same, i != full.FULL_INDEX, i)
            self.assertEqual(after[1][56:72], old[1][56:72])
        entry = geometry(outputs['primary'], LBAS, 0)[2][31 * 128:32 * 128]
        self.assertEqual(uuid.UUID(bytes_le=entry[16:32]), uuid.UUID(int=9))
        self.assertEqual(entry[56:78].decode('utf-16-le'), 'macossystem')

    def test_refusals(self):
        with self.assertRaisesRegex(ValueError, 'at least 16 GiB'):
            full.layout_full(self.primary, self.secondary, LBAS, 8 * GIB)
        with self.assertRaisesRegex(ValueError, 'whole number of MiB'):
            full.layout_full(self.primary, self.secondary, LBAS, 64 * GIB + 4096)
        with self.assertRaisesRegex(ValueError, 'does not fit'):
            full.layout_full(self.primary, self.secondary, LBAS, 1024 * GIB)
        with self.assertRaisesRegex(ValueError, 'nonzero'):
            full.layout_full(self.primary, self.secondary, LBAS, 64 * GIB, uuid.UUID(int=0))
        original = fixture.windows_from_entries(fixture.synthetic_entries())
        with self.assertRaisesRegex(ValueError, 'entries 29-31'):
            full.layout_full(*original, LBAS, 64 * GIB)
        once, _ = full.layout_full(self.primary, self.secondary, LBAS, 64 * GIB)
        with self.assertRaisesRegex(ValueError, 'entry 32'):
            full.layout_full(once['primary'], once['secondary'], LBAS, 64 * GIB)
        with self.assertRaisesRegex(ValueError, 'disagree'):
            full.layout_full(once['primary'], self.secondary, LBAS, 64 * GIB)

    def test_prepare_requires_the_verified_step4_gpt(self):
        device = FakeDevice(self.primary, self.secondary)
        plan = full.prepare_full(device, self.dir / 'plan', 96 * GIB, self.step4)
        self.assertEqual(full.validate_prepared_full(self.dir / 'plan')['full']['bytes'], 96 * GIB)
        self.assertEqual(device.writes, [])
        changed = FakeDevice(*fixture.windows_from_entries(fixture.synthetic_entries()))
        with self.assertRaisesRegex(ValueError, 'Step-4'):
            full.prepare_full(changed, self.dir / 'other', 96 * GIB, self.step4)
        (self.dir / 'plan/after-primary.bin').write_bytes(self.primary)
        with self.assertRaisesRegex(ValueError, 'after-GPT'):
            full.validate_prepared_full(self.dir / 'plan', plan)

    def test_apply_writes_backup_then_primary_and_verifies(self):
        device = FakeDevice(self.primary, self.secondary)
        full.prepare_full(device, self.dir / 'plan', 96 * GIB, self.step4)
        preserved = self.dir / 'preserved'
        record = full.apply_full(device, self.dir / 'plan', preserved)
        self.assertTrue(record['applied'] and record['gpt_readback_verified'])
        self.assertEqual([kind for kind, _, _ in device.writes],
                         ['write-secondary-array.bin', 'write-secondary-header.bin',
                          'write-primary-array.bin', 'write-primary-header.bin'])
        self.assertEqual(device.writes[3][1:], (1, 1))
        self.assertEqual(device.read(0, cn.ALIGN), (self.dir / 'plan/after-primary.bin').read_bytes())
        self.assertEqual((preserved / 'verified-primary.bin').read_bytes(), device.read(0, cn.ALIGN))
        journal = [json.loads(line) for line in (self.dir / 'plan/apply.jsonl').read_text().splitlines()]
        self.assertEqual([entry['operation'] for entry in journal], ['starting', 'verified'] * 4)
        with self.assertRaisesRegex(ValueError, 'already applied'):
            full.apply_full(device, self.dir / 'plan', preserved)

    def test_apply_refuses_a_changed_device(self):
        device = FakeDevice(self.primary, self.secondary)
        full.prepare_full(device, self.dir / 'plan', 96 * GIB, self.step4)
        device.blocks[2] = b'\x01' * cn.BLOCK
        with self.assertRaisesRegex(ValueError, 'changed after preparation'):
            full.apply_full(device, self.dir / 'plan', self.dir / 'preserved')
        self.assertEqual(device.writes, [])
        self.assertFalse((self.dir / 'plan/apply.jsonl').exists())


if __name__ == '__main__':
    unittest.main()
