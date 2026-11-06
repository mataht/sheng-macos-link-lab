# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from capture_android_userdata_format import parse
from install_macos26_ufs_root import qualify_android_format


class AndroidUserdataFormat(unittest.TestCase):
    def setUp(self):
        self.plan = dict(root_name='macosroot', root_index=31, root_first_lba=36665600, root_last_lba=45054207,
                         root_unique_guid='11111111-2222-3333-4444-555555555555', userdata_bytes=128 << 30)
        self.outputs = dict(
            mounts='/dev/block/dm-48 /data f2fs rw,lazytime 0 0\n/dev/block/sda12 /metadata ext4 rw 0 0',
            slaves='sda29', userdata_size=str((128 << 30) // 512),
            root_start=str(36665600 * 8), root_size=str((45054207 - 36665600 + 1) * 8),
            root_uevent='MAJOR=8\nMINOR=31\nDEVNAME=sda31\nDEVTYPE=partition\nPARTN=31\nPARTNAME=macosroot',
            statfs=f'4096 {(120 << 30) // 4096}', setup='0')

    def test_rebuilt_userdata_qualifies_for_the_installer(self):
        evidence = parse('test-serial', self.plan, self.outputs)
        self.assertEqual((evidence['physical_partition'], evidence['physical_disk']), ('sda29', 'sda'))
        self.assertEqual(evidence['userdata_partition_bytes'], 128 << 30)
        self.assertFalse(evidence['device_writes'])
        qualify_android_format(evidence, self.plan, 'test-serial')

    def test_mismatches_are_refused(self):
        for key, value, message in (('root_uevent', 'PARTNAME=userdata', 'macosroot node'),
                                    ('root_start', '0', 'macosroot node'),
                                    ('mounts', '', 'one /data')):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, message):
                    parse('test-serial', self.plan, dict(self.outputs, **{key: value}))
        # A setup-complete device, a non-F2FS filesystem or a userdata size mismatch fails the installer gate.
        for key, value in (('setup', '1'), ('mounts', '/dev/block/dm-48 /data ext4 rw 0 0'),
                           ('userdata_size', str((64 << 30) // 512))):
            with self.subTest(gate=key):
                evidence = parse('test-serial', self.plan, dict(self.outputs, **{key: value}))
                with self.assertRaisesRegex(ValueError, 'rebuilt Android filesystem'):
                    qualify_android_format(evidence, self.plan, 'test-serial')


if __name__ == '__main__':
    unittest.main()
