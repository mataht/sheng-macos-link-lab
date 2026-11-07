# SPDX-License-Identifier: GPL-3.0-or-later
from contextlib import redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from capture_recovery_userdata_format import COMMANDS, FSTAB_SHA256, RECOVERY_SHA256, main, parse, qualify_recovery_format


def make_raw(plan_bytes=None, serial='test-serial'):
    if plan_bytes is None:
        plan = dict(layout='sheng-cn-1tb-lun0', disk='sda', logical_block_bytes=4096,
                    root_name='macosroot', root_index=31, root_first_lba=36665600, root_last_lba=45054207,
                    root_unique_guid='11111111-2222-3333-4444-555555555555', root_bytes=32 << 30,
                    userdata_bytes=128 << 30, serial=serial, applied=True, gpt_readback_verified=True,
                    userdata=dict(index=29, first_lba=3094592, last_lba=36649023, bytes=128 << 30))
        plan_bytes = json.dumps(plan, indent=2).encode()
    else:
        plan = json.loads(plan_bytes)
    values = dict(state='recovery', uid='2000', device='sheng', serial=serial, twrp='', bootmode='unknown',
                  fingerprint='Xiaomi/sheng/sheng:13/test/OS3.0.306.0.WNXCNXM:user/release-keys',
                  boot_id='249121bf-db02-43b5-92bb-2b422958f4d4', uptime='215.76 1717.98',
                  boot_id_end='249121bf-db02-43b5-92bb-2b422958f4d4', uptime_end='216.27 1722.01',
                  mounts='/dev/block/bootdevice/by-name/userdata /data f2fs rw 0 0',
                  userdata_node='/dev/block/sda29', userdata_start=str(plan['userdata']['first_lba'] * 8),
                  userdata_size=str(plan['userdata_bytes'] // 512),
                  userdata_uevent='DEVNAME=sda29\nDEVTYPE=partition\nPARTN=29\nPARTNAME=userdata',
                  root_node='/dev/block/sda31', root_start=str(plan['root_first_lba'] * 8),
                  root_size=str(plan['root_bytes'] // 512),
                  root_uevent='DEVNAME=sda31\nDEVTYPE=partition\nPARTN=31\nPARTNAME=macosroot',
                  statfs='4096 ' + str((plan['userdata_bytes'] - (2 << 20)) // 4096), df='fixture-only',
                  recovery_binary_sha256=RECOVERY_SHA256 + '  /system/bin/recovery',
                  recovery_fstab_sha256=FSTAB_SHA256 + '  /system/etc/recovery.fstab')
    raw = dict(schema='sheng-stock-recovery-raw-v1', serial=serial, device_writes=False,
               plan_sha256=hashlib.sha256(plan_bytes).hexdigest(), applied_plan=plan,
               captured_start_utc='2026-11-07T00:29:25.285091+00:00',
               captured_end_utc='2026-11-07T00:29:26.155175+00:00',
               user_reported_wipe_completed=dict(value=True, source='unit-test fixture', text='fixture report'),
               outputs={key: dict(command=list(command), returncode=0, stdout=values[key] + '\r\n', stderr='')
                        for key, command in COMMANDS.items()})
    return plan_bytes, raw


def encode_raw(raw):
    return json.dumps(raw, indent=2).replace('\n', '\r\n').encode('utf-8')


class StockRecoveryFormat(unittest.TestCase):
    def setUp(self):
        self.plan_bytes, self.raw = make_raw()

    def parse(self, raw=None):
        return parse('test-serial', self.plan_bytes, encode_raw(self.raw if raw is None else raw))

    def test_direct_bounded_f2fs_is_qualified_without_claiming_android_or_log(self):
        evidence = self.parse()
        self.assertEqual(evidence['filesystem_bytes'], (128 << 30) - (2 << 20))
        self.assertEqual(evidence['raw_capture_utf8'].encode('utf-8'), encode_raw(self.raw))
        self.assertEqual(evidence['raw_capture_sha256'], hashlib.sha256(encode_raw(self.raw)).hexdigest())
        self.assertEqual(evidence['evidence_kind'], 'stock-recovery-filesystem')
        self.assertFalse(evidence['recovery_wipe_log_observed'])
        self.assertFalse(evidence['android_initialization_observed'])
        self.assertTrue(evidence['format_pending_vold'])
        self.assertNotIn('user_setup_complete', evidence)
        qualify_recovery_format(evidence, self.plan_bytes, 'test-serial')

    def test_failed_missing_or_mislabeled_command_is_rejected(self):
        for change in ('failed', 'missing', 'mislabeled', 'missing-stderr'):
            raw = deepcopy(self.raw)
            if change == 'failed':
                raw['outputs']['statfs']['returncode'] = 1
            elif change == 'missing':
                del raw['outputs']['boot_id_end']
            elif change == 'mislabeled':
                raw['outputs']['userdata_node']['command'] = ['shell', 'readlink -f /another/node']
            else:
                del raw['outputs']['statfs']['stderr']
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.parse(raw)

    def test_wrong_identity_or_mount_source_is_rejected(self):
        for key, value in (('state', 'device'), ('device', 'another-device'), ('serial', 'another-serial'),
                           ('twrp', '3.7'), ('uid', '0'), ('recovery_binary_sha256', '00' * 32),
                           ('recovery_fstab_sha256', '00' * 32), ('userdata_node', '/dev/block/sda30'),
                           ('root_node', '/dev/block/sda30'),
                           ('mounts', '/dev/block/dm-0 /data f2fs rw 0 0'),
                           ('mounts', '/dev/block/by-name/userdata /data f2fs rw 0 0'),
                           ('mounts', '/dev/block/bootdevice/by-name/userdata /data ext4 rw 0 0'),
                           ('mounts', self.raw['outputs']['mounts']['stdout'] * 2)):
            raw = deepcopy(self.raw)
            raw['outputs'][key]['stdout'] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.parse(raw)

    def test_incorrect_partition_identity_extent_and_filesystem_are_rejected(self):
        for key, value in (('userdata_start', '0'), ('userdata_size', '268435455'),
                           ('root_start', '293324801'), ('root_size', '67108865'),
                           ('userdata_uevent', 'PARTNAME=another-name'),
                           ('root_uevent', 'PARTNAME=userdata'),
                           ('statfs', '4096 33554433'), ('statfs', '4096 0'),
                           ('statfs', '4096 33553920e0'), ('statfs', '512 268435456')):
            raw = deepcopy(self.raw)
            raw['outputs'][key]['stdout'] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.parse(raw)

    def test_boot_capture_and_user_report_binding_are_required(self):
        for change in ('boot', 'uptime', 'interval', 'report'):
            raw = deepcopy(self.raw)
            if change == 'boot':
                raw['outputs']['boot_id_end']['stdout'] = '11111111-2222-3333-4444-555555555555'
            elif change == 'uptime':
                raw['outputs']['uptime_end']['stdout'] = '10.0 100.0'
            elif change == 'interval':
                raw['captured_end_utc'] = '2026-11-06T00:00:00+00:00'
            else:
                raw['user_reported_wipe_completed']['value'] = False
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.parse(raw)

    def test_exact_applied_plan_bytes_and_serial_are_required(self):
        with self.assertRaises(ValueError):
            parse('test-serial', self.plan_bytes + b'\n', encode_raw(self.raw))
        with self.assertRaises(ValueError):
            parse('another-serial', self.plan_bytes, encode_raw(self.raw))
        raw = deepcopy(self.raw)
        raw['applied_plan']['root_unique_guid'] = 'another-guid'
        with self.assertRaises(ValueError):
            self.parse(raw)

    def test_derived_field_or_raw_capture_tampering_cannot_qualify(self):
        valid = self.parse()
        for key, value in (('filesystem_bytes', 1), ('boot_id', 'another-boot'), ('read_only', 1),
                           ('raw_capture_sha256', '00' * 32), ('android_initialization_observed', True),
                           ('root_partition_guid', 'another-root'), ('schema', 'android-filesystem')):
            with self.subTest(key=key), self.assertRaises(ValueError):
                qualify_recovery_format(dict(valid, **{key: value}), self.plan_bytes, 'test-serial')
        raw = deepcopy(self.raw)
        raw['outputs']['statfs']['stdout'] = '4096 268435456'
        changed = dict(valid, raw_capture_utf8=encode_raw(raw).decode(),
                       raw_capture_sha256=hashlib.sha256(encode_raw(raw)).hexdigest(), filesystem_bytes=1 << 40)
        with self.assertRaises(ValueError):
            qualify_recovery_format(changed, self.plan_bytes, 'test-serial')

    def test_cli_preserves_source_bytes_and_refuses_to_overwrite_output(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            (folder / 'plan.json').write_bytes(self.plan_bytes)
            source, output = folder / 'raw.json', folder / 'evidence.json'
            source.write_bytes(encode_raw(self.raw))
            args = ['capture_recovery_userdata_format.py', '--serial', 'test-serial', '--partition-plan', str(folder),
                    '--raw-input', str(source), '--output', str(output)]
            with patch.object(sys, 'argv', args), redirect_stdout(io.StringIO()):
                main()
            evidence = json.loads(output.read_bytes())
            qualify_recovery_format(evidence, self.plan_bytes, 'test-serial')
            self.assertEqual(source.read_bytes(), encode_raw(self.raw))
            self.assertEqual((folder / 'plan.json').read_bytes(), self.plan_bytes)
            saved = output.read_bytes()
            with patch.object(sys, 'argv', args), self.assertRaises(FileExistsError):
                main()
            self.assertEqual(output.read_bytes(), saved)


if __name__ == '__main__':
    unittest.main()
