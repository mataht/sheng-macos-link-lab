# SPDX-License-Identifier: GPL-3.0-or-later
import hashlib
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stderr
import io

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from install_macos26_ufs_root import install, main, PARTITION
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

    def test_recovery_log_requires_both_markers_and_exact_size(self):
        original = self.device.shell
        size = self.plan['userdata_bytes']
        prefix = 'format_volume: wipe metadata encrypted /dev/block/bootdevice/by-name/userdata with size '
        invalid = {
            'missing-completion': prefix + str(size),
            'missing-size': 'Data wipe complete.',
            'wrong-size': 'Data wipe complete.\n' + prefix + str(size - 1),
            'extra-zero': 'Data wipe complete.\n' + prefix + str(size) + '0\n',
            'extra-digits': prefix + str(size) + '123\nData wipe complete.',
            'leading-digit': 'Data wipe complete.\n' + prefix + '1' + str(size),
            'leading-zero': 'Data wipe complete.\n' + prefix + '0' + str(size),
            'hex-suffix': 'Data wipe complete.\n' + prefix + str(size) + 'x01',
            'exponent-suffix': 'Data wipe complete.\n' + prefix + str(size) + 'e0',
            'fractional-suffix': 'Data wipe complete.\n' + prefix + str(size) + '.0',
        }
        for label, log in invalid.items():
            with self.subTest(label=label):
                self.device.commands.clear()
                def shell(command, **kwargs):
                    if command == 'cat /tmp/recovery.log':
                        self.device.commands.append(command)
                        return log
                    return original(command, **kwargs)
                self.device.shell = shell
                with patch('install_macos26_ufs_root.subprocess.run') as run:
                    with self.assertRaisesRegex(ValueError, 'completed Recovery wipe'):
                        install(self.device, self.plan_dir, self.root_dir, self.output)
                run.assert_not_called()
                self.assertFalse(any(c.startswith('blkdiscard') for c in self.device.commands))
                self.assertFalse(self.output.exists())

    def test_recovery_log_size_followed_by_whitespace_qualifies(self):
        original = self.device.shell
        def shell(command, **kwargs):
            if command == 'cat /tmp/recovery.log':
                self.device.commands.append(command)
                return ('format_volume: wipe metadata encrypted /dev/block/bootdevice/by-name/userdata '
                        f'with size {self.plan["userdata_bytes"]}\nData wipe complete.')
            return original(command, **kwargs)
        self.device.shell = shell
        with patch('install_macos26_ufs_root.subprocess.run') as run:
            record = install(self.device, self.plan_dir, self.root_dir, self.output)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(record['status'], 'verified')

    def test_recovery_log_records_only_the_observed_wipe_route(self):
        # The existing fixture deliberately has completion before the size marker: no new ordering assumption.
        with patch('install_macos26_ufs_root.subprocess.run') as run:
            record = install(self.device, self.plan_dir, self.root_dir, self.output)
        self.assertEqual(run.call_count, 1)
        evidence = record['userdata_reset_evidence']
        self.assertEqual((evidence['kind'], evidence['source']), ('recovery-log', '/tmp/recovery.log'))
        self.assertEqual(evidence['capture'], 'decoded-adb-shell-output')
        self.assertTrue(evidence['recovery_wipe_observed'])
        self.assertEqual(evidence['artifact_sha256'],
                         hashlib.sha256((self.output / 'recovery-wipe.log').read_bytes()).hexdigest())
        self.assertFalse((self.output / 'android-format.json').exists())
        updated = json.loads((self.plan_dir / 'plan.json').read_text())
        self.assertTrue(updated['userdata_recovery_wipe_completed'])
        self.assertFalse(updated['userdata_filesystem_rebuilt'])
        self.assertTrue(updated['userdata_format_pending_vold'])

    def android_evidence(self, **changes):
        evidence = dict(serial=self.device.serial, read_only=True,
                        root_partition_guid=self.plan['root_unique_guid'],
                        userdata_partition_bytes=self.plan['userdata_bytes'], user_setup_complete='0',
                        filesystem='f2fs', physical_partition='sda29', physical_disk='sda',
                        filesystem_bytes=self.plan['userdata_bytes'] - (2 << 20))
        evidence.update(changes)
        path = self.folder / 'android-observation.json'
        path.write_bytes(json.dumps(evidence, indent=2).replace('\n', '\r\n').encode('utf-8'))
        return path

    def android_shell_without_recovery_log(self, userdata_bytes=None):
        original = self.device.shell
        def shell(command, **kwargs):
            if command == 'cat /tmp/recovery.log':
                raise AssertionError('Android evidence must not depend on Recovery log availability')
            if command == 'blockdev --getsize64 /dev/block/by-name/userdata':
                self.device.commands.append(command)
                return str(self.plan['userdata_bytes'] if userdata_bytes is None else userdata_bytes)
            return original(command, **kwargs)
        self.device.shell = shell

    def test_android_route_works_without_recovery_log_and_preserves_evidence(self):
        source = self.android_evidence()
        captured = source.read_bytes()
        self.android_shell_without_recovery_log()
        with patch('install_macos26_ufs_root.subprocess.run') as run:
            record = install(self.device, self.plan_dir, self.root_dir, self.output, source)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(record['status'], 'verified')
        self.assertNotIn('cat /tmp/recovery.log', self.device.commands)
        self.assertFalse((self.output / 'recovery-wipe.log').exists())
        self.assertEqual((self.output / 'android-format.json').read_bytes(), captured)
        evidence = record['userdata_reset_evidence']
        self.assertEqual(evidence['kind'], 'android-filesystem')
        self.assertEqual(evidence['capture'], 'local-file-bytes')
        self.assertFalse(evidence['recovery_wipe_observed'])
        self.assertEqual(evidence['artifact_sha256'], hashlib.sha256(captured).hexdigest())
        updated = json.loads((self.plan_dir / 'plan.json').read_text())
        self.assertIsNone(updated['userdata_recovery_wipe_completed'])
        self.assertEqual(updated['userdata_reset_evidence_kind'], 'android-filesystem')
        self.assertTrue(updated['userdata_filesystem_rebuilt'])
        self.assertFalse(updated['userdata_format_pending_vold'])

    def test_invalid_android_evidence_refuses_discard_and_write_without_log_fallback(self):
        source = self.android_evidence(serial='another-device')
        self.android_shell_without_recovery_log()
        with patch('install_macos26_ufs_root.subprocess.run') as run:
            with self.assertRaisesRegex(ValueError, 'rebuilt Android filesystem'):
                install(self.device, self.plan_dir, self.root_dir, self.output, source)
        run.assert_not_called()
        self.assertFalse(any(c.startswith('blkdiscard') for c in self.device.commands))
        self.assertFalse(self.output.exists())

    def test_android_route_keeps_current_userdata_size_gate(self):
        source = self.android_evidence()
        self.android_shell_without_recovery_log(userdata_bytes=self.plan['userdata_bytes'] - 4096)
        with patch('install_macos26_ufs_root.subprocess.run') as run:
            with self.assertRaisesRegex(ValueError, 'current userdata node differs'):
                install(self.device, self.plan_dir, self.root_dir, self.output, source)
        run.assert_not_called()
        self.assertFalse(any(c.startswith('blkdiscard') for c in self.device.commands))
        self.assertFalse(self.output.exists())

    def test_filesystem_evidence_routes_are_mutually_exclusive_before_device_access(self):
        missing = self.folder / 'not-read.json'
        with self.assertRaisesRegex(ValueError, 'mutually exclusive'):
            install(self.device, self.plan_dir, self.root_dir, self.output, missing, missing)
        self.assertEqual(self.device.commands, [])
        args = ['install_macos26_ufs_root.py', '--serial', self.device.serial,
                '--partition-plan', str(self.plan_dir), '--root-build', str(self.root_dir), '--output', str(self.output),
                '--android-format', str(missing), '--recovery-format', str(missing)]
        with patch.object(sys, 'argv', args), patch('install_macos26_ufs_root.Device') as device, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                main()
        self.assertEqual(error.exception.code, 2)
        device.assert_not_called()

    def discard_tools(self, named=True, toybox=True, discard_failure=None):
        original = self.device.shell
        probes = {'blkdiscard --help': named, 'toybox blkdiscard --help': toybox}
        def shell(command, **kwargs):
            if command in probes:
                # Tool selection must finish before even creating a deployment directory.
                self.assertFalse(self.output.exists())
                self.assertNotIn(PARTITION, command)
                self.device.commands.append(command)
                if not probes[command]:
                    raise subprocess.CalledProcessError(127, command)
                return 'usage: blkdiscard DEVICE'
            if command in ('blkdiscard ' + PARTITION, 'toybox blkdiscard ' + PARTITION):
                self.device.commands.append(command)
                saved = json.loads((self.output / 'deployment.json').read_bytes())
                self.assertEqual(saved['discard_attempts'], 1)
                self.assertEqual(saved['image_write_attempts'], 0)
                self.assertEqual(saved['discard_command'], command)
                if discard_failure is not None:
                    raise discard_failure(command)
                return ''
            return original(command, **kwargs)
        self.device.shell = shell

    def test_named_discard_tool_is_selected_without_probing_toybox(self):
        self.discard_tools()
        with patch('install_macos26_ufs_root.subprocess.run') as run:
            record = install(self.device, self.plan_dir, self.root_dir, self.output)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(record['discard_command'], 'blkdiscard ' + PARTITION)
        self.assertEqual(record['discard_attempts'], 1)
        self.assertEqual(record['discard_returncode'], 0)
        self.assertEqual(record['discard_tool_probes'], [dict(command='blkdiscard --help', returncode=0)])
        self.assertNotIn('toybox blkdiscard --help', self.device.commands)
        self.assertEqual(self.device.commands.count('blkdiscard ' + PARTITION), 1)

    def test_toybox_discard_is_selected_only_after_named_help_fails(self):
        self.discard_tools(named=False)
        with patch('install_macos26_ufs_root.subprocess.run') as run:
            record = install(self.device, self.plan_dir, self.root_dir, self.output)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(record['discard_command'], 'toybox blkdiscard ' + PARTITION)
        self.assertEqual(record['discard_attempts'], 1)
        self.assertEqual(record['discard_returncode'], 0)
        self.assertEqual(record['discard_tool_probes'], [dict(command='blkdiscard --help', returncode=127),
                                                       dict(command='toybox blkdiscard --help', returncode=0)])
        self.assertNotIn('blkdiscard ' + PARTITION, self.device.commands)
        self.assertEqual(self.device.commands.count('toybox blkdiscard ' + PARTITION), 1)

    def test_no_discard_tool_refuses_before_deployment_directory_or_write(self):
        self.discard_tools(named=False, toybox=False)
        plan_before = (self.plan_dir / 'plan.json').read_bytes()
        with patch('install_macos26_ufs_root.subprocess.run') as run:
            with self.assertRaisesRegex(ValueError, 'no usable blkdiscard tool'):
                install(self.device, self.plan_dir, self.root_dir, self.output)
        run.assert_not_called()
        self.assertFalse(self.output.exists())
        self.assertNotIn('blkdiscard ' + PARTITION, self.device.commands)
        self.assertNotIn('toybox blkdiscard ' + PARTITION, self.device.commands)
        self.assertEqual((self.plan_dir / 'plan.json').read_bytes(), plan_before)

    def test_actual_discard_failure_records_one_attempt_and_never_falls_back_or_writes(self):
        self.discard_tools(discard_failure=lambda command: subprocess.CalledProcessError(1, command))
        plan_before = (self.plan_dir / 'plan.json').read_bytes()
        with patch('install_macos26_ufs_root.subprocess.run') as run:
            with self.assertRaises(subprocess.CalledProcessError):
                install(self.device, self.plan_dir, self.root_dir, self.output)
        run.assert_not_called()
        self.assertEqual(self.device.commands.count('blkdiscard ' + PARTITION), 1)
        self.assertFalse(any(c.startswith('toybox blkdiscard') for c in self.device.commands))
        self.assertFalse((self.output / 'write.log').exists())
        record = json.loads((self.output / 'deployment.json').read_bytes())
        self.assertEqual(record['status'], 'discard-failed')
        self.assertEqual(record['discard_command'], 'blkdiscard ' + PARTITION)
        self.assertEqual(record['discard_attempts'], 1)
        self.assertEqual(record['discard_returncode'], 1)
        self.assertEqual(record['discard_error']['type'], 'CalledProcessError')
        self.assertEqual(record['image_write_attempts'], 0)
        self.assertNotIn('readback_verified', record)
        self.assertEqual((self.plan_dir / 'plan.json').read_bytes(), plan_before)


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

    def recovery_evidence(self):
        from test_capture_recovery_userdata_format import make_raw, encode_raw
        from capture_recovery_userdata_format import parse
        plan_bytes, raw = make_raw((self.plan_dir / 'plan.json').read_bytes(), self.device.serial)
        evidence = parse(self.device.serial, plan_bytes, encode_raw(raw))
        source = self.folder / 'recovery-observation.json'
        source.write_bytes(json.dumps(evidence, indent=2).replace('\n', '\r\n').encode())
        return source

    def test_recovery_filesystem_route_preserves_evidence_and_pending_android_state(self):
        source = self.recovery_evidence()
        saved = source.read_bytes()
        self.android_shell_without_recovery_log()
        with patch('install_macos26_ufs_root.subprocess.run') as run:
            record = install(self.device, self.plan_dir, self.root_dir, self.output, recovery_format=source)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(record['status'], 'verified')
        self.assertNotIn('cat /tmp/recovery.log', self.device.commands)
        self.assertFalse((self.output / 'recovery-wipe.log').exists())
        self.assertEqual((self.output / 'recovery-format.json').read_bytes(), saved)
        evidence = record['userdata_reset_evidence']
        self.assertEqual(evidence['kind'], 'stock-recovery-filesystem')
        self.assertFalse(evidence['recovery_wipe_observed'])
        self.assertEqual(evidence['artifact_sha256'], hashlib.sha256(saved).hexdigest())
        updated = json.loads((self.plan_dir / 'plan.json').read_bytes())
        self.assertIsNone(updated['userdata_recovery_wipe_completed'])
        self.assertTrue(updated['userdata_filesystem_rebuilt'])
        self.assertTrue(updated['userdata_format_pending_vold'])
        self.assertFalse(updated['userdata_android_initialization_observed'])

    def test_recovery_derived_tampering_refuses_discard_and_write(self):
        source = self.recovery_evidence()
        evidence = json.loads(source.read_bytes())
        evidence['filesystem_bytes'] = 1
        source.write_text(json.dumps(evidence))
        self.android_shell_without_recovery_log()
        with patch('install_macos26_ufs_root.subprocess.run') as run:
            with self.assertRaisesRegex(ValueError, 'derived fields'):
                install(self.device, self.plan_dir, self.root_dir, self.output, recovery_format=source)
        run.assert_not_called()
        self.assertFalse(any(c.startswith('blkdiscard') for c in self.device.commands))
        self.assertFalse(self.output.exists())

    def test_recovery_evidence_from_an_older_plan_refuses_discard_and_write(self):
        source = self.recovery_evidence()
        (self.plan_dir / 'plan.json').write_bytes((self.plan_dir / 'plan.json').read_bytes() + b'\n')
        self.android_shell_without_recovery_log()
        with patch('install_macos26_ufs_root.subprocess.run') as run:
            with self.assertRaisesRegex(ValueError, 'bound to the applied CN plan'):
                install(self.device, self.plan_dir, self.root_dir, self.output, recovery_format=source)
        run.assert_not_called()
        self.assertFalse(any(c.startswith('blkdiscard') for c in self.device.commands))
        self.assertFalse(self.output.exists())

    def test_recovery_route_keeps_current_userdata_size_gate(self):
        source = self.recovery_evidence()
        self.android_shell_without_recovery_log(userdata_bytes=self.plan['userdata_bytes'] - 4096)
        with patch('install_macos26_ufs_root.subprocess.run') as run:
            with self.assertRaisesRegex(ValueError, 'current userdata node differs'):
                install(self.device, self.plan_dir, self.root_dir, self.output, recovery_format=source)
        run.assert_not_called()
        self.assertFalse(any(c.startswith('blkdiscard') for c in self.device.commands))
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
