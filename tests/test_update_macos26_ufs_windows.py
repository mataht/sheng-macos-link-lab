# SPDX-License-Identifier: GPL-3.0-or-later
"""Keep root updates confined to ISC, with the new manifest committed last."""
import copy
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import update_macos26_ufs_windows as updater
from prepare_macos26_ufs_root import prepare, MIB
from repartition_macos26_root import BLOCK, WINDOW, layout
import test_repartition_macos26_root as partition_fixture


class UpdateWindows(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.plan_dir, self.previous, self.candidate = [self.folder / n for n in ('plan', 'old', 'new')]
        self.plan_dir.mkdir()
        self.candidate.mkdir()
        primary, secondary, lbas = partition_fixture.RepartitionRoot().fixture()
        copies, self.plan = layout(primary, secondary, lbas, 64)
        self.plan.update(applied=True, gpt_readback_verified=True, serial='29d9c0be')
        for name, data in copies.items():
            (self.plan_dir / f'after-{name}.bin').write_bytes(data)
        source, evidence = self.folder / 'root.raw', self.folder / 'root.json'
        source.write_bytes(b'R' * 512)
        root_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        evidence.write_text(json.dumps(dict(build='25F71', bytes=512, sha256=root_hash, read_only_qemu_input=True)))
        self.old = prepare(source, evidence, self.previous)
        self.isc = b'I' * MIB
        self.contract = dict(bytes=512, sha256=root_hash, isc=dict(bytes=MIB, sha256=hashlib.sha256(self.isc).hexdigest()))
        config = self.folder / 'config'
        config.mkdir()
        (config / 'macos26-recovery-root.json').write_text(json.dumps(self.contract))
        self.new = dict(self.old, image_bytes=3*MIB, isc_volatile_overlay=True,
            windows=self.old['windows']+[dict(name='ISC', offset=2*MIB, bytes=MIB, sha256=self.contract['isc']['sha256'])])
        header = bytearray(BLOCK)
        header[:8] = b'SHNGROOT'
        struct.pack_into('<IIQ', header, 8, 1, 2, 3*MIB)
        for i, window in enumerate(self.new['windows']):
            struct.pack_into('<32sQQII32s40s', header, 24+i*128, window['name'].encode(), window['offset'],
                window['bytes'], 512, 1, bytes.fromhex(window['sha256']), bytes(40))
        header[4064:] = hashlib.sha256(header[:4064]).digest()
        self.new['manifest_sha256'] = hashlib.sha256(header).hexdigest()
        data = bytearray(3*MIB)
        data[:BLOCK] = header
        data[MIB:MIB+512] = source.read_bytes()
        data[2*MIB:] = self.isc
        (self.candidate / 'macosroot.img').write_bytes(data)
        (self.candidate / 'manifest.bin').write_bytes(header)
        (self.candidate / 'build.json').write_text(json.dumps(self.new))
        self.current = bytearray((self.previous / 'macosroot.img').read_bytes())+bytearray(MIB)
        self.parent_manifest = bytes(self.current[:BLOCK])
        old_digest = hashlib.sha256(self.current[:2*MIB]).hexdigest()
        self.plan['root_image_sha256'] = old_digest
        (self.plan_dir / 'plan.json').write_text(json.dumps(self.plan))
        self.installed = self.folder / 'installed.json'
        self.installed.write_text(json.dumps(dict(status='verified', readback_verified=True,
            destination=updater.PARTITION, destination_guid=self.plan['root_unique_guid'],
            sha256=old_digest, readback_sha256=old_digest)))
        owner = self
        class Device:
            serial, adb = '29d9c0be', ['adb', '-s', '29d9c0be']
            def __init__(self):
                self.lbas, self.fail_isc_hash, self.commands = lbas, False, []
            def require_recovery(self):
                pass
            def read(self, start, count):
                if start == owner.plan['root_first_lba']:
                    assert count == 1
                    return owner.parent_manifest
                return copies['primary' if start == 0 else 'secondary']
            def shell(self, command, **kwargs):
                self.commands.append(command)
                if '--flushbufs' in command:
                    owner.parent_manifest = bytes(owner.current[:BLOCK])
                    return ''
                if command.startswith('readlink'):
                    return '/dev/block/sda'+str(owner.plan['root_index'])
                if command.startswith('blockdev --getsize64'):
                    return str(owner.plan['root_bytes'])
                if 'sha256sum' in command:
                    if 'skip=1048576 count=512 ' in command:
                        data = owner.current[MIB:MIB+512]
                    elif 'skip=2097152 ' in command:
                        if self.fail_isc_hash:
                            return '0'*64+'  -'
                        data = owner.current[2*MIB:]
                    else:
                        data = owner.current
                    return hashlib.sha256(data).hexdigest()+'  -'
                return ''
        self.device = Device()
        self.writes = []

    def remote_write(self, arguments, **kwargs):
        command = arguments[-1]
        self.assertIn('of='+updater.PARTITION, command)
        self.assertRegex(command, r'iflag=(?:skip_bytes,)?count_bytes ')
        self.assertNotIn('blkdiscard', command)
        staged = 'if=/tmp/' in command
        if staged:
            self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
            self.assertNotIn('input', kwargs)
            data = (self.candidate / 'macosroot.img').read_bytes()
        if 'seek=2 ' in command:
            self.writes.append('ISC')
            self.assertIn(f'bs={MIB} ', command)
            self.assertIn(f'count={MIB} ', command)
            if staged:
                self.assertIn(f'skip={2*MIB} ', command)
                self.assertIn('iflag=skip_bytes,count_bytes ', command)
            self.current[2*MIB:] = data[2*MIB:] if staged else kwargs['stdin'].read(MIB)
        else:
            self.writes.append('manifest')
            self.assertIn(f'bs={BLOCK} count={BLOCK} ', command)
            self.assertNotIn('seek=', command)
            self.assertNotIn('skip=', command)
            self.current[:BLOCK] = data[:BLOCK] if staged else kwargs['input']

    def run_update(self):
        with patch.object(updater, 'ROOT', self.folder), patch.object(updater.subprocess, 'run', side_effect=self.remote_write):
            return updater.update(self.device, self.plan_dir, self.previous, self.installed, self.candidate, self.folder/'deployment')

    def test_commits_manifest_after_isc_and_preserves_base_system(self):
        before = bytes(self.current[MIB:2*MIB])
        record = self.run_update()
        self.assertEqual(self.writes, ['ISC', 'manifest'])
        self.assertEqual(self.current[MIB:2*MIB], before)
        self.assertTrue(record['readback_verified'])
        self.assertTrue(record['manifest_committed'])
        self.assertFalse(record['base_system_written'])
        self.assertEqual(sum('--flushbufs' in command for command in self.device.commands), 3)

    def test_bad_isc_readback_keeps_original_manifest_without_retry(self):
        header = bytes(self.current[:BLOCK])
        self.device.fail_isc_hash = True
        with self.assertRaisesRegex(ValueError, 'original manifest retained'):
            self.run_update()
        self.assertEqual(self.writes, ['ISC'])
        self.assertEqual(self.current[:BLOCK], header)
        record = json.loads((self.folder/'deployment/deployment.json').read_text())
        self.assertFalse(record['manifest_committed'])
        self.assertEqual(record['image_write_attempts'], 1)

    def test_overlapping_or_foreign_seed_is_refused(self):
        for key, value in (('offset', MIB), ('bytes', MIB*2), ('sha256', '0'*64)):
            candidate = copy.deepcopy(self.new)
            candidate['windows'][1][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'ISC extent'):
                updater.validate_update(self.old, candidate, self.plan, self.contract)

    def test_unknown_manifest_ack_is_recorded_without_retry(self):
        write = self.remote_write
        def disconnected(arguments, **kwargs):
            write(arguments, **kwargs)
            if 'input' in kwargs:
                raise subprocess.TimeoutExpired(arguments, 30)
        with patch.object(updater, 'ROOT', self.folder), patch.object(updater.subprocess, 'run', side_effect=disconnected):
            with self.assertRaises(subprocess.TimeoutExpired):
                updater.update(self.device, self.plan_dir, self.previous, self.installed,
                               self.candidate, self.folder/'deployment')
        record = json.loads((self.folder/'deployment/deployment.json').read_text())
        self.assertEqual(self.writes, ['ISC', 'manifest'])
        self.assertIsNone(record['manifest_committed'])
        self.assertEqual(record['failed_phase'], 'committing-manifest')

    def test_modified_candidate_payload_is_refused_before_writes(self):
        image = self.candidate/'macosroot.img'
        with image.open('r+b') as stream:
            stream.seek(2*MIB)
            stream.write(b'!')
        with self.assertRaisesRegex(ValueError, 'candidate payload'):
            self.run_update()
        self.assertEqual(self.writes, [])

    def staged_ram_image(self, *, mounts=None, realpath=None, size=None, digest=None, regular=True):
        remote = '/tmp/sheng-macosroot-update-test/macosroot.img'
        image = self.candidate / 'macosroot.img'
        original = self.device.shell
        expected = hashlib.sha256(image.read_bytes()).hexdigest()
        values = {
            'cat /proc/mounts': 'tmpfs /tmp tmpfs rw 0 0' if mounts is None else mounts,
            'readlink -f ' + remote: remote if realpath is None else realpath,
            'stat -c %s ' + remote: str(image.stat().st_size if size is None else size),
            'sha256sum ' + remote: (expected if digest is None else digest) + '  ' + remote,
        }
        def shell(command, **kwargs):
            if command == 'test -f ' + remote:
                self.device.commands.append(command)
                if not regular:
                    raise subprocess.CalledProcessError(1, command)
                return ''
            if command in values:
                self.device.commands.append(command)
                return values[command]
            return original(command, **kwargs)
        self.device.shell = shell
        return remote

    def run_staged_update(self, remote, *, output='deployment', writer=None):
        with patch.object(updater, 'ROOT', self.folder), \
                patch('install_macos26_ufs_root.sys.platform', 'win32'), \
                patch.object(updater.subprocess, 'run', side_effect=writer or self.remote_write):
            return updater.update(self.device, self.plan_dir, self.previous, self.installed,
                                  self.candidate, self.folder / output, staged_image=remote)

    def test_windows_staging_writes_only_isc_then_verified_manifest_without_binary_stdin(self):
        remote = self.staged_ram_image()
        before = bytes(self.current)
        def checked_write(arguments, **kwargs):
            command = arguments[-1]
            self.assertIn('sha256sum ' + remote, self.device.commands)
            saved = json.loads((self.folder / 'deployment/deployment.json').read_text())
            if 'seek=' not in command:
                self.assertTrue(saved['isc_readback_verified'])
                self.assertIsNone(saved['manifest_committed'])
                self.assertEqual(saved['manifest_write_attempts'], 1)
                self.assertFalse(saved['manifest_readback_verified'])
                self.assertEqual(self.writes, ['ISC'])
            self.remote_write(arguments, **kwargs)
        record = self.run_staged_update(remote, writer=checked_write)
        self.assertEqual(self.writes, ['ISC', 'manifest'])
        self.assertEqual(bytes(self.current[BLOCK:2*MIB]), before[BLOCK:2*MIB])
        self.assertTrue(record['manifest_readback_verified'])
        self.assertEqual(record['image_write_transport'], 'staged-ram-file')
        self.assertEqual(record['staged_image']['sha256'], record['sha256'])
        self.assertEqual(record['image_write_attempts'], 1)
        self.assertEqual(record['manifest_write_attempts'], 1)
        self.assertFalse(record['gpt_writes'])
        self.assertFalse(record['base_system_written'])
        self.assertFalse(any('blkdiscard' in command or 'dd of=/dev/block/sda' in command
                             for command in self.device.commands))

    def test_unqualified_staging_refuses_before_any_write_or_deployment(self):
        original = self.device.shell
        plan_before = (self.plan_dir / 'plan.json').read_bytes()
        before = bytes(self.current)
        for change in (dict(digest='00'*32), dict(size=1), dict(regular=False),
                       dict(mounts='/dev/block/sda29 /tmp ext4 rw 0 0'),
                       dict(realpath='/data/macosroot.img'),
                       dict(mounts='tmpfs /tmp tmpfs rw 0 0\n/dev/block/sda29 /tmp/sheng-macosroot-update-test ext4 rw 0 0'),
                       dict(mounts='tmpfs /tmp tmpfs rw 0 0\n/dev/block/sda29 /tmp/sheng-macosroot-update-test/macosroot.img ext4 rw 0 0')):
            with self.subTest(change=change):
                self.device.shell = original
                remote = self.staged_ram_image(**change)
                with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                    self.run_staged_update(remote)
                self.assertFalse((self.folder / 'deployment').exists())
                self.assertEqual(self.writes, [])
                self.assertEqual(bytes(self.current), before)
                self.assertEqual((self.plan_dir / 'plan.json').read_bytes(), plan_before)

    def test_windows_missing_stage_refuses_before_device_construction(self):
        args = ['update_macos26_ufs_windows.py', '--serial', self.device.serial,
                '--partition-plan', str(self.plan_dir), '--previous-root', str(self.previous),
                '--previous-deployment', str(self.installed), '--candidate-root', str(self.candidate),
                '--output', str(self.folder/'deployment')]
        with patch('install_macos26_ufs_root.sys.platform', 'win32'):
            with self.assertRaisesRegex(ValueError, 'Windows ADB stdin'):
                updater.update(self.device, self.plan_dir, self.previous, self.installed,
                               self.candidate, self.folder/'deployment')
            with patch.object(sys, 'argv', args), patch.object(updater, 'Device') as device:
                with self.assertRaisesRegex(ValueError, 'Windows ADB stdin'):
                    updater.main()
                device.assert_not_called()
        self.assertEqual(self.device.commands, [])
        self.assertFalse((self.folder/'deployment').exists())

    def test_unsafe_staging_path_refuses_before_device_access(self):
        for remote in ('/data/macosroot.img', '/tmp/sheng-macosroot-x/../macosroot.img',
                       '/tmp/sheng-macosroot-x/macosroot.img;id', '/tmp/sheng-macosroot-x/macosroot.img\nid'):
            with self.subTest(remote=remote), self.assertRaisesRegex(ValueError, 'safe-id'):
                self.run_staged_update(remote)
        self.assertEqual(self.device.commands, [])
        self.assertFalse((self.folder/'deployment').exists())

    def test_staged_failures_preserve_one_attempt_and_do_not_update_plan(self):
        remote = self.staged_ram_image()
        plan_before = (self.plan_dir / 'plan.json').read_bytes()
        for phase in ('ISC', 'manifest'):
            for kind in ('nonzero', 'timeout', 'oserror'):
                label = phase + '-' + kind
                self.writes.clear()
                errors = dict(nonzero=subprocess.CalledProcessError(1, 'adb shell dd'),
                              timeout=subprocess.TimeoutExpired('adb shell dd', 30),
                              oserror=OSError('transport unavailable'))
                error = errors[kind]
                def failed_write(arguments, **kwargs):
                    self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
                    self.assertNotIn('input', kwargs)
                    which = 'ISC' if 'seek=' in arguments[-1] else 'manifest'
                    if which == phase:
                        self.writes.append(which)
                        raise error
                    self.remote_write(arguments, **kwargs)
                with self.subTest(phase=phase, kind=kind), self.assertRaises(type(error)):
                    self.run_staged_update(remote, output=label, writer=failed_write)
                record = json.loads((self.folder/label/'deployment.json').read_text())
                self.assertEqual(record['status'], 'failed')
                self.assertEqual(record['image_write_attempts'], 1)
                self.assertEqual(record['manifest_write_attempts'], int(phase == 'manifest'))
                self.assertEqual(record['manifest_committed'], None if phase == 'manifest' else False)
                self.assertFalse(record['manifest_readback_verified'])
                self.assertEqual(self.writes, ['ISC', 'manifest'] if phase == 'manifest' else ['ISC'])
                self.assertEqual((self.plan_dir / 'plan.json').read_bytes(), plan_before)

    def test_manifest_readback_mismatch_is_unknown_and_never_retried(self):
        remote = self.staged_ram_image()
        plan_before = (self.plan_dir/'plan.json').read_bytes()
        def corrupt_write(arguments, **kwargs):
            self.remote_write(arguments, **kwargs)
            if 'seek=' not in arguments[-1]:
                self.current[0] ^= 1
        with self.assertRaisesRegex(ValueError, 'manifest readback differs'):
            self.run_staged_update(remote, writer=corrupt_write)
        record = json.loads((self.folder/'deployment/deployment.json').read_text())
        self.assertEqual(self.writes, ['ISC', 'manifest'])
        self.assertEqual(record['failed_phase'], 'verifying-manifest')
        self.assertIsNone(record['manifest_committed'])
        self.assertEqual(record['manifest_write_returncode'], 0)
        self.assertFalse(record['manifest_readback_verified'])
        self.assertEqual((self.plan_dir/'plan.json').read_bytes(), plan_before)

    def test_staged_manifest_ack_loss_keeps_actual_write_uncertain(self):
        remote = self.staged_ram_image()
        plan_before = (self.plan_dir/'plan.json').read_bytes()
        def lost_ack(arguments, **kwargs):
            self.remote_write(arguments, **kwargs)
            if 'seek=' not in arguments[-1]:
                raise subprocess.TimeoutExpired(arguments, 30)
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_staged_update(remote, writer=lost_ack)
        record = json.loads((self.folder/'deployment/deployment.json').read_text())
        self.assertEqual(self.writes, ['ISC', 'manifest'])
        self.assertEqual(bytes(self.current[:BLOCK]), (self.candidate/'manifest.bin').read_bytes())
        self.assertIsNone(record['manifest_committed'])
        self.assertIsNone(record['manifest_write_returncode'])
        self.assertFalse(record['manifest_readback_verified'])
        self.assertEqual(record['manifest_write_attempts'], 1)
        self.assertEqual((self.plan_dir/'plan.json').read_bytes(), plan_before)

    def test_existing_output_is_preserved_without_a_new_write(self):
        output = self.folder/'deployment'
        output.mkdir()
        marker = output/'deployment.json'
        marker.write_text('{"status":"failed"}')
        remote = self.staged_ram_image()
        with self.assertRaises(FileExistsError):
            self.run_staged_update(remote)
        self.assertEqual(marker.read_text(), '{"status":"failed"}')
        self.assertEqual(self.writes, [])


class UpdateWindowsCn(UpdateWindows):
    """Apply the same byte-range and failure assertions to the CN 1 TB plan."""

    def setUp(self):
        import repartition_sheng_cn_1tb as cn
        import test_repartition_sheng_cn_1tb as cn_fixture
        super().setUp()
        primary, secondary = cn_fixture.windows_from_entries(cn_fixture.synthetic_entries())
        copies, plan = cn.layout(primary, secondary, cn.LUN0_LBAS)
        plan.update(applied=True, gpt_readback_verified=True, serial='00000000',
                    root_image_sha256=self.plan['root_image_sha256'])
        self.plan = plan
        (self.plan_dir/'plan.json').write_text(json.dumps(plan))
        for kind, raw in copies.items():
            (self.plan_dir/f'after-{kind}.bin').write_bytes(raw)
        installed = json.loads(self.installed.read_text())
        installed['destination_guid'] = plan['root_unique_guid']
        self.installed.write_text(json.dumps(installed))
        self.device.serial, self.device.lbas = plan['serial'], cn.LUN0_LBAS
        self.device.adb = ['adb', '-s', plan['serial']]
        def read(start, count):
            if start == plan['root_first_lba']:
                self.assertEqual(count, 1)
                return self.parent_manifest
            return copies['primary' if start == 0 else 'secondary']
        self.device.read = read

    def test_cli_reuses_the_qualified_cn_device_class(self):
        from install_macos26_ufs_root import Device
        from repartition_sheng_cn_1tb import Device as CnDevice
        self.assertIs(updater.Device, Device)
        self.assertIs(updater.Device, CnDevice)
        self.assertEqual((self.plan['root_index'], self.plan['root_bytes']), (31, 32 << 30))


if __name__ == '__main__':
    unittest.main()
