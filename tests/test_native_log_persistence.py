# SPDX-License-Identifier: GPL-3.0-or-later
import os
import re
from pathlib import Path
import shutil
import subprocess
import struct
import zlib
import sys
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from capture_persistent_log import RAM_RANGES, inspect_capture, request, status_record
from native_log_file import LAYOUT


SAVER_PATCHES = ('0033-retained-log-high-bank-and-filesystem.patch', '0042-native-log-saver-cn-layout.patch')


def patched_saver(work: Path) -> Path:
    """Assemble the reviewed UEFI saver header: 0015 creates it, the later patches modify it."""
    patch = (ROOT / 'patches/UniXNU/0015-native-retained-log-persistence.patch').read_text()
    addition = patch.split('+++ b/drivers/DarwinBoot/DarwinBootPkg/Driver/NativeLogSave.h\n', 1)[1]
    header = ''.join(line[1:] for line in addition.splitlines(keepends=True)
                     if line.startswith('+') and not line.startswith('+++'))
    target = work / 'drivers/DarwinBoot/DarwinBootPkg/Driver/NativeLogSave.h'
    target.parent.mkdir(parents=True)
    target.write_text(header)
    for name in SAVER_PATCHES:
        subprocess.run(['patch', '--batch', '-p1', '-i', str(ROOT / 'patches/UniXNU' / name)],
                       cwd=work, check=True, stdout=subprocess.DEVNULL)
    return target


class NativeLogPersistence(unittest.TestCase):
    def test_saver_constants_match_layout_and_capture_ranges(self):
        if not shutil.which('patch'):
            self.skipTest('requires patch')
        with tempfile.TemporaryDirectory() as temporary:
            saver = patched_saver(Path(temporary)).read_text()
        self.assertIn(f"#define NATIVE_LOG_PARTITION_FIRST {LAYOUT['partition_first']}ULL", saver)
        self.assertIn(f"#define NATIVE_LOG_PARTITION_LAST {LAYOUT['partition_last']}ULL", saver)
        self.assertIn(f"#define NATIVE_LOG_PARTITION_LAST_BLOCK {LAYOUT['partition_blocks'] - 1}ULL", saver)
        guid = uuid.UUID(LAYOUT['partition_uuid'])
        literal = '{0x%08x, 0x%04x, 0x%04x, {%s}}' % (guid.fields[0], guid.fields[1], guid.fields[2],
                                                     ', '.join('0x%02x' % b for b in guid.bytes[8:]))
        self.assertIn('mNativeLogPartition =\n  ' + literal + ';', saver)
        ram = re.findall(r'\{(0x[0-9a-f]+)ULL, (0x[0-9a-f]+)ULL\}', saver)
        self.assertEqual(tuple(tuple(int(v, 16) for v in pair) for pair in ram), RAM_RANGES)
        for first, end in RAM_RANGES:
            self.assertEqual((first & 0x3fff, end & 0x3fff), (0, 0))
        self.assertTrue(all(a[1] < b[0] for a, b in zip(RAM_RANGES, RAM_RANGES[1:])))

    def test_real_saver_with_mock_efi_services(self):
        clang = os.environ.get('CLANG', shutil.which('clang'))
        include = ROOT / 'uefi-sheng/Mu_Basecore/MdePkg/Include'
        if not clang or not (include / 'Uefi.h').is_file():
            self.skipTest('requires host clang and the pinned EDK2 headers')
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            target = patched_saver(work)
            shutil.copy2(target, work / 'NativeLogSave.h')
            executable = work / 'saver'
            subprocess.run([clang, '-fshort-wchar', '-Wall', '-Wextra', '-Werror',
                            '-Wno-unused-function', '-Wno-missing-field-initializers',
                            '-I' + str(include), '-I' + str(include / 'X64'), '-I' + str(work),
                            str(ROOT / 'tests/native_log_save_host.c'), '-o', str(executable)], check=True)
            volume = work / 'macoslog'
            volume.mkdir()
            subprocess.run([str(executable), str(volume)], check=True)
            for capture in ('CAP00001', 'CAP00002', 'CAP00005', 'CAP00006', 'CAP00007'):
                info = inspect_capture(volume / capture, '62626262-6262-6262-6262-626262626262')
                self.assertTrue(info['internal_file_capture'])
                self.assertFalse(info['warm_reboot_retention_qualified'])
            self.assertFalse(info['native']['complete'])
            self.assertEqual(info['native']['omitted_bytes'], 18081)
            for capture in ('CAP00003', 'CAP00004'):
                self.assertFalse((volume / capture / 'DONE.TXT').exists())
            self.assertEqual(info['request']['log_pa'], '0x8c1004000')
            self.assertEqual(info['request']['panic_pa'], '0x981004000')
            for name, magic in (('RECOVERY.BIN', b'SHNREC01'), ('ARM.BIN', b'SHNARM01')):
                self.assertEqual(status_record((volume / name).read_bytes(), magic)['name'], 'Success')
            with self.assertRaises(ValueError):
                status_record(bytes(16), b'SHNREC01')
            raw = (volume / 'CAP00001/REQUEST.BIN').read_bytes()
            self.assertEqual(request(raw)['log_pa'], '0x881004000')
            for address in (0x8b8700000, 0x8ba700000, 0x8bb300000, 0x8bb500000, 0xbffffc000, 0xc00000000,
                            0xffffffffffffc000):
                invalid = bytearray(raw)
                struct.pack_into('<Q', invalid, 16, address)
                struct.pack_into('<I', invalid, 72, 0)
                struct.pack_into('<I', invalid, 72, zlib.crc32(invalid))
                with self.assertRaisesRegex(ValueError, 'RAM bounds'):
                    request(invalid)
            for address in (0x8b8900000, 0x9ffffc000, 0xbfffe8000):
                valid = bytearray(raw)
                struct.pack_into('<Q', valid, 16, address)
                struct.pack_into('<I', valid, 72, 0)
                struct.pack_into('<I', valid, 72, zlib.crc32(valid))
                self.assertEqual(request(bytes(valid))['log_pa'], hex(address))
            with self.assertRaises(ValueError):
                request(raw[:-1])
            with self.assertRaises(ValueError):
                inspect_capture(volume / 'CAP00001', '00000000-0000-0000-0000-000000000001')


if __name__ == '__main__':
    unittest.main()
