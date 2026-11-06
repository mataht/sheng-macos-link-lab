# SPDX-License-Identifier: GPL-3.0-or-later
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from install_macos26_ufs_root import install, PARTITION
from prepare_macos26_ufs_root import prepare
from repartition_macos26_root import BLOCK, WINDOW, layout
import test_repartition_macos26_root as partition_fixture


class InstallUfsRoot(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.plan_dir, self.root_dir = self.folder / 'plan', self.folder / 'root'
        self.output = self.folder / 'deployment'
        self.plan_dir.mkdir()
        primary, secondary, lbas = partition_fixture.RepartitionRoot().fixture()
        copies, self.plan = layout(primary, secondary, lbas, 64)
        self.plan.update(applied=True, gpt_readback_verified=True, serial='29d9c0be')
        for kind, data in copies.items():
            (self.plan_dir / f'after-{kind}.bin').write_bytes(data)
        (self.plan_dir / 'plan.json').write_text(json.dumps(self.plan))
        raw = self.folder / 'source.raw'
        raw.write_bytes(b'X' * 512)
        evidence = self.folder / 'source.json'
        evidence.write_text(json.dumps(dict(build='25F71', bytes=512,
            sha256=hashlib.sha256(raw.read_bytes()).hexdigest(), read_only_qemu_input=True)))
        prepare(raw, evidence, self.root_dir)
        root_digest = hashlib.sha256((self.root_dir / 'macosroot.img').read_bytes()).hexdigest()
        plan = self.plan

        class Device:
            serial, adb = '29d9c0be', ['adb', '-s', '29d9c0be']
            def __init__(self):
                self.lbas, self.digest, self.copies, self.commands = lbas, root_digest, copies, []
            def require_recovery(self):
                pass
            def read(self, start, count):
                assert count == WINDOW // BLOCK
                return self.copies['primary' if start == 0 else 'secondary']
            def shell(self, command, **kwargs):
                self.commands.append(command)
                if command.startswith('readlink'):
                    return '/dev/block/sda' + str(plan['root_index'])
                if command.startswith('blockdev --getsize64'):
                    return str(plan['root_bytes'])
                if command == 'cat /tmp/recovery.log':
                    return ('Data wipe complete.\nformat_volume: wipe metadata encrypted '
                            '/dev/block/bootdevice/by-name/userdata with size ' + str(plan['userdata_bytes']))
                if 'sha256sum' in command:
                    return self.digest + '  -'
                return ''
        self.device = Device()

    def test_stale_gpt_refuses_discard_and_write(self):
        self.device.copies = dict(self.device.copies, primary=bytes(WINDOW))
        with patch('install_macos26_ufs_root.subprocess.run') as run:
            with self.assertRaisesRegex(ValueError, 'layout changed'):
                install(self.device, self.plan_dir, self.root_dir, self.output)
        run.assert_not_called()
        self.assertFalse(any(c.startswith('blkdiscard') for c in self.device.commands))
        self.assertFalse(self.output.exists())

    def test_mismatched_readback_has_one_remote_write_and_no_success(self):
        self.device.digest = '00' * 32
        with patch('install_macos26_ufs_root.subprocess.run') as run:
            with self.assertRaisesRegex(ValueError, 'no automatic retry'):
                install(self.device, self.plan_dir, self.root_dir, self.output)
        self.assertEqual(run.call_count, 1)
        command = run.call_args.args[0]
        self.assertEqual(command[3:5], ['shell', '-T'])
        self.assertEqual(command[5], 'dd of=' + PARTITION + ' bs=1048576 conv=notrunc,fsync')
        record = json.loads((self.output / 'deployment.json').read_text())
        self.assertEqual(record['status'], 'readback-mismatch')
        self.assertEqual(record['image_write_attempts'], 1)
        self.assertNotIn('readback_verified', record)
        self.assertFalse(json.loads((self.plan_dir / 'plan.json').read_text())['root_image_written'])


class InstallUfsRootCn(InstallUfsRoot):
    """The same installer on the CN 1 TB LUN0 plan (repartition_sheng_cn_1tb.py)."""

    def setUp(self):
        import repartition_sheng_cn_1tb as cn
        import test_repartition_sheng_cn_1tb as cn_fixture
        super().setUp()
        primary, secondary = cn_fixture.windows_from_entries(cn_fixture.synthetic_entries())
        copies, plan = cn.layout(primary, secondary, cn.LUN0_LBAS)
        plan.update(applied=True, gpt_readback_verified=True, serial=self.device.serial)
        for kind, data in copies.items():
            (self.plan_dir / f'after-{kind}.bin').write_bytes(data)
        (self.plan_dir / 'plan.json').write_text(json.dumps(plan))
        self.plan = plan
        self.device.lbas, self.device.copies = cn.LUN0_LBAS, copies
        device = self.device
        original = type(device).shell

        def shell(command, **kwargs):
            if command.startswith('readlink'):
                device.commands.append(command)
                return '/dev/block/sda' + str(plan['root_index'])
            if command.startswith('blockdev --getsize64'):
                device.commands.append(command)
                return str(plan['root_bytes'])
            if command == 'cat /tmp/recovery.log':
                device.commands.append(command)
                return ('Data wipe complete.\nformat_volume: wipe metadata encrypted '
                        '/dev/block/bootdevice/by-name/userdata with size ' + str(plan['userdata_bytes']))
            return original(device, command, **kwargs)
        device.shell = shell

    def test_cn_plan_fields_match_the_partition_it_describes(self):
        self.assertEqual((self.plan['root_index'], self.plan['root_name']), (31, 'macosroot'))
        self.assertEqual((self.plan['root_first_lba'], self.plan['root_last_lba']),
                         (self.plan['macosroot']['first_lba'], self.plan['macosroot']['last_lba']))
        self.assertEqual(self.plan['root_unique_guid'], self.plan['macosroot']['unique_guid'])
        self.assertEqual((self.plan['root_bytes'], self.plan['userdata_bytes']), (32 << 30, 128 << 30))

    def test_verified_write_updates_the_cn_plan(self):
        with patch('install_macos26_ufs_root.subprocess.run') as run:
            record = install(self.device, self.plan_dir, self.root_dir, self.output)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(record['status'], 'verified')
        self.assertEqual(record['destination_guid'], self.plan['root_unique_guid'])
        updated = json.loads((self.plan_dir / 'plan.json').read_text())
        self.assertTrue(updated['root_image_readback_verified'])
        self.assertEqual(updated['root_image_sha256'], record['sha256'])
        self.assertIn('readlink -f ' + PARTITION, self.device.commands)


if __name__ == '__main__':
    unittest.main()
