# SPDX-License-Identifier: GPL-3.0-or-later
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / 'platform/sheng/macos26/input'
sys.path.insert(0, str(ROOT / 'scripts'))


class NativeButtonKeys(unittest.TestCase):
    def test_spmi_logic_against_a_simulated_arbiter(self):
        compiler = shutil.which('c++') or shutil.which('clang++')
        if compiler is None:
            self.skipTest('requires a host C++ compiler')
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / 'keys'
            subprocess.run([compiler, '-std=c++17', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined',
                            str(ROOT / 'tests/native_button_keys.cpp'), '-o', str(executable)],
                           check=True, capture_output=True, timeout=120)
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('PASS', result.stdout)

    def test_driver_is_default_off_read_only_and_hardware_only(self):
        source = (INPUT / 'native_button_keys.cpp').read_text()
        header = (INPUT / 'spmi_keys.h').read_text()
        self.assertIn('PE_parse_boot_argn("native-button-keys", &enabled, sizeof(enabled))', source)
        self.assertIn('enabled != 1', source)
        self.assertIn('PE_vmm_present != 0', source)
        # Only the observer (read) channel is ever written; no write channel, no PMIC write opcode.
        self.assertNotIn('chnls', source.lower().replace('no chnls', ''))
        self.assertEqual(re.findall(r'kOp\w+ = \d+', header), ['kOpExtReadLong = 1'])
        self.assertIn('writeObs(channel + kCmd, readCommand(address))', header)
        # The physical regions mapped are the arbiter's own (from the sheng DT), read-only where possible.
        for region in ('0x0c400000', '0x0c42d000', '0x0c440000'):
            self.assertIn(region, source)

    def test_storage_build_stages_apple_hid_private_headers_for_button_keys(self):
        import build_macos26_storage

        with tempfile.TemporaryDirectory() as temporary:
            temp_root = Path(temporary) / 'root'
            out = Path(temporary) / 'out'
            out.mkdir()
            for rel in ('config/macos26-recovery-root.json', 'config/macoslog.json',
                        'platform/sheng/macos26/input/native_button_keys.cpp',
                        'platform/sheng/macos26/input/spmi_keys.h',
                        'platform/sheng/macos26/storage/macoslog_layout.h',
                        'platform/sheng/macos26/storage/macoslog_worker.h',
                        'platform/sheng/macos26/storage/recovery_root.h'):
                dst = temp_root / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes((ROOT / rel).read_bytes())
            for rel in ('UniXNU/drivers/NativeUfs/NativeUfs.cpp',
                        'UniXNU/drivers/NativeUfs/RootPublication.h',
                        'UniXNU/drivers/NativeUfs/RootWindowOverlay.h',
                        'UniXNU/drivers/NativeRamBlock/SparseDisk.h'):
                dst = temp_root / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text('// stub\n')
            (temp_root / 'sources/macos26/IOStorageFamily').mkdir(parents=True)

            manifest = json.loads((ROOT / 'config/macos26-input-reference.json').read_text())
            headers = {}
            sibling_inputs = out / 'ufs-driver/recovery-input/inputs'
            sibling_inputs.mkdir(parents=True)
            for name, entry in manifest['apple_headers']['files'].items():
                data = f'// {name}\n'.encode()
                entry['canonical_sha256'] = hashlib.sha256(data).hexdigest()
                (sibling_inputs / name).write_bytes(data)
                headers[name] = data
            (temp_root / 'config/macos26-input-reference.json').write_text(json.dumps(manifest))

            commands = {}
            sdk = Path('/Applications/Xcode_27.1.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX27.0.sdk')

            def record_run(command, label, record, cwd=None, env=None):
                commands[label] = list(command)
                if label == 'ufs-driver-link':
                    Path(command[command.index('-o') + 1]).write_bytes(b'macho')

            compile_record = {
                'directory': str(out),
                'file': 'IOPlatformExpert.cpp',
                'arguments': ['clang++', '-c', 'IOPlatformExpert.cpp', '-o', 'IOPlatformExpert.o'],
            }
            record = {}
            with (patch('prepare_macos26_gop.prepare', return_value={'source': 'NativeGOPDisplay.cpp'}),
                  patch('subprocess.check_output', return_value=str(sdk) + '\n'),
                  patch('build_macos26_storage.macho', return_value={
                      'kind': 11, 'subtype': 0x40000002, 'commands': [(0x80000034, 0, 0)]}),
                  patch('urllib.request.urlopen', side_effect=AssertionError('unexpected download'))):
                build_macos26_storage.build(
                    temp_root, out, compile_record, out / 'source', record_run, record, {},
                    transport='ufs', gop_display=True, recovery_input=False, button_keys=True)

            keys_dir = out / 'ufs-driver/button-keys'
            for name, data in headers.items():
                self.assertEqual((keys_dir / 'include/IOKit/hid' / name).read_bytes(), data)
            compile_cmd = commands['ufs-driver-button-keys-compile']
            self.assertIn(str(keys_dir / 'include'), compile_cmd)
            inc_idx = compile_cmd.index(str(keys_dir / 'include'))
            self.assertEqual(compile_cmd[inc_idx - 1], '-I')
            self.assertIn(str(sdk / 'System/Library/Frameworks/Kernel.framework/Versions/A/Headers'), compile_cmd)
            self.assertNotIn(str(sdk / 'usr/include'), compile_cmd)

    def test_virtio_bundle_carries_only_the_qemu_display_and_input(self):
        import build_macos26_storage
        import plistlib

        with tempfile.TemporaryDirectory() as temporary:
            temp_root, out = Path(temporary) / 'root', Path(temporary) / 'out'
            out.mkdir()
            upstream = temp_root / 'UniXNU/drivers/NativeVirtioBlock/NativeVirtioBlock.cpp'
            upstream.parent.mkdir(parents=True)
            upstream.write_text('// stub\n')
            (temp_root / 'sources/macos26/IOStorageFamily').mkdir(parents=True)
            sdk = Path('/sdk')
            commands = {}

            def record_run(command, label, record, cwd=None, env=None):
                commands[label] = list(command)
                if label.endswith('-link'):
                    Path(command[command.index('-o') + 1]).write_bytes(b'macho')

            compile_record = dict(directory=str(out), file='IOPlatformExpert.cpp',
                                  arguments=['clang++', '-c', 'IOPlatformExpert.cpp', '-o', 'IOPlatformExpert.o'])
            record = {}
            with (patch('prepare_macos26_gop.prepare', return_value={'source': 'NativeGOPDisplay.cpp'}),
                  patch('prepare_macos26_input.prepare', return_value={'source': 'NativeRecoveryInput.cpp'}),
                  patch('subprocess.check_output', return_value=str(sdk) + '\n'),
                  patch('build_macos26_storage.macho', return_value={
                      'kind': 11, 'subtype': 0x40000002, 'commands': [(0x80000034, 0, 0)]})):
                for refused in (dict(button_keys=True), dict(touch=True)):
                    with self.subTest(refused=refused), self.assertRaisesRegex(ValueError, 'UFS bundle'):
                        build_macos26_storage.build(temp_root, out, compile_record, out / 'source', record_run, {}, {},
                                                    gop_display=True, **refused)
                build_macos26_storage.build(temp_root, out, compile_record, out / 'source', record_run, record, {},
                                            gop_display=True, recovery_input=True)
            evidence = record['storage_driver']
            self.assertIs(evidence['qemu_only_display'], True)
            self.assertIs(evidence['explicit_qemu_scratch_writes'], True)
            self.assertIs(evidence['hardware_admitted'], False)
            self.assertNotIn('ufs_driver', record)
            info = plistlib.loads((out / 'storage-driver/Info.plist').read_bytes())
            self.assertEqual(set(info['IOKitPersonalities']), {'NativeVirtioBlock', 'NativeGOPDisplay',
                                                               'NativeRecoveryInput', 'NativeRecoveryHIDEventDriver'})
            self.assertIn('#include "NativeGOPDisplay.cpp"',
                          (out / 'storage-driver/NativeVirtioBlock.cpp').read_text())
            self.assertIn('-DNATIVE_QEMU_SCRATCH_BLOCK=1', commands['storage-driver-compile'])
            self.assertIn('storage-driver-input-compile', commands)


if __name__ == '__main__':
    unittest.main()
