# SPDX-License-Identifier: GPL-3.0-or-later
"""Keyboard cover companion: imported codec tests, the front end against simulated hardware, contract, device-tree node, packager arguments."""
import contextlib
import hashlib
import io
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import native_keyboard  # noqa: E402

INPUT = ROOT / 'platform/sheng/macos26/input'
IMPORTED = INPUT / 'keyboard'
HEADERS = ('IOKit/IOBufferMemoryDescriptor.h', 'IOKit/IODeviceMemory.h', 'IOKit/IODeviceTreeSupport.h', 'IOKit/IOLib.h',
           'IOKit/IOMemoryDescriptor.h', 'IOKit/IOService.h', 'IOKit/hid/IOHIDDevice.h', 'kern/clock.h', 'kern/thread.h',
           'libkern/c++/OSData.h', 'libkern/libkern.h', 'pexpert/pexpert.h')
FIRMWARE = bytes(0x7fc0) + b'NANO IC' + bytes(82524 - 0x7fc7)


class NativeKeyboard(unittest.TestCase):
    def tool(self, name):
        found = shutil.which(name)
        if not found:
            self.skipTest('requires ' + name)
        return found

    def run_host(self, temporary, name, *sources):
        executable = Path(temporary) / name
        subprocess.run([self.tool('cc'), '-std=c11', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined', *map(str, sources),
                        '-o', str(executable)], check=True, capture_output=True, timeout=120)
        result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(result.stdout.startswith('PASS'), result.stdout)

    def test_imported_codec_and_i2c_core_pass_their_own_tests(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.run_host(temporary, 'wn8030', IMPORTED / 'tests/wn8030_host.c', IMPORTED / 'Wn8030.c')
            self.run_host(temporary, 'geni', IMPORTED / 'tests/geni_i2c_host.c', IMPORTED / 'GeniI2c.c')

    def test_imported_files_are_unmodified_and_recorded(self):
        record = (IMPORTED / 'UPSTREAM.md').read_text()
        self.assertIn(native_keyboard.IMPORTED_COMMIT, record)
        for name in (*native_keyboard.IMPORTED_C, *native_keyboard.IMPORTED_HEADERS):
            self.assertIn('SPDX-License-Identifier: BSD-2-Clause-Patent', (IMPORTED / name).read_text().splitlines()[0])
        self.assertIn('Redistribution', (IMPORTED / 'LICENSE').read_text())

    def test_front_end_against_simulated_hardware(self):
        compiler = self.tool('c++')
        board = ROOT / 'UniXNU-sheng/package'
        if not (board / 'TouchBoard.h').exists():
            self.skipTest('upstream checkout not present')
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            for header in HEADERS:
                path = base / 'include' / header
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('#include "native_keyboard_stubs.h"\n')
            objects = []
            for name in native_keyboard.COMPILED_C:
                target = base / (name + '.o')
                subprocess.run([self.tool('cc'), '-std=c11', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined', '-c',
                                str(IMPORTED / name), '-o', str(target)], check=True, capture_output=True, timeout=120)
                objects.append(str(target))
            # The imported I2C core and RPMh builder export only private names, so another companion's copy of them cannot collide at link time.
            names = ' '.join(subprocess.run(['nm', '-g', '--defined-only', object_file], check=True, capture_output=True, text=True).stdout
                             for object_file in objects)
            self.assertNotRegex(names, r'\b(geni_i2c_|qcom_)')
            self.assertIn('keyboard_geni_i2c_poll', names)
            self.assertIn('keyboard_cmd_db_find', names)
            executable = base / 'keyboard'
            build = subprocess.run([compiler, '-std=c++17', '-Wall', '-Wextra', '-Werror', '-Wno-subobject-linkage', '-fsanitize=address,undefined',
                                    '-I', str(ROOT / 'tests'), '-I', str(base / 'include'), '-I', str(INPUT), '-I', str(IMPORTED), '-I', str(board),
                                    str(ROOT / 'tests/native_keyboard_host.cpp'), *objects, '-o', str(executable)],
                                   capture_output=True, text=True, timeout=180)
            self.assertEqual(build.returncode, 0, build.stderr[-4000:])
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=120, env={'ASAN_OPTIONS': 'detect_leaks=0'})
            self.assertEqual(result.returncode, 0, result.stdout[-4000:] + result.stderr[-2000:])
            self.assertIn('PASS', result.stdout)

    def test_driver_is_default_off_hardware_only_and_has_no_weak_imports(self):
        source = (INPUT / 'native_keyboard.cpp').read_text()
        self.assertIn('!PE_parse_boot_argn("sheng-kbd", &fStage, sizeof(fStage)) || fStage < 1 || fStage > 5', source)
        self.assertIn('PE_vmm_present != 0', source)
        self.assertNotIn('weak_import', source)
        for name in (*native_keyboard.IMPORTED_C, *native_keyboard.IMPORTED_HEADERS):
            self.assertNotIn('weak_import', (IMPORTED / name).read_text())
        self.assertNotIn('armcpu_spi', source)   # the interrupt lines stay with their owners
        self.assertNotIn('devauth', source)
        self.assertIn('fStage != 4 || fFrames > kFramesLogged', source)    # a frame is printed only at stage 4
        self.assertEqual(re.findall(r'\{"ldob12", \d+\}, \{"ldob13", \d+\}', source), ['{"ldob12", 1800}, {"ldob13", 3296}'])
        self.assertIn('fRails && !setupRails()', source)

    def test_contract(self):
        record = native_keyboard.evidence()
        native_keyboard.validate(record)
        self.assertFalse(record['firmware_compiled_in'] or record['hardware_qualified'] or record['authentication_answered'])
        for key, value in (('default_off', False), ('rail_votes_default_off', False), ('storage_writes', True)):
            changed = dict(record, **{key: value})
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'contract'):
                native_keyboard.validate(changed)
        with self.assertRaisesRegex(ValueError, 'contract'):
            native_keyboard.validate(dict(record, source_sha256={}))

    def test_firmware_pin(self):
        with mock.patch.object(native_keyboard, 'FIRMWARE_SHA256', hashlib.sha256(FIRMWARE).hexdigest()):
            properties = native_keyboard.node_properties(FIRMWARE)
            self.assertEqual(properties['firmware'], FIRMWARE)
            self.assertTrue(all(isinstance(value, bytes) for value in properties.values()))
            with self.assertRaisesRegex(ValueError, 'pinned'):
                native_keyboard.node_properties(FIRMWARE[:-1])
            with self.assertRaisesRegex(ValueError, 'pinned'):
                native_keyboard.node_properties(FIRMWARE[:-1] + b'\1')
        with self.assertRaisesRegex(ValueError, 'pinned'):
            native_keyboard.node_properties(FIRMWARE)

    def test_boot_arguments(self):
        self.assertEqual(native_keyboard.boot_arguments(1), ['sheng-kbd=1'])
        self.assertEqual(native_keyboard.boot_arguments(5, rails=True, khz=100, poll_read_ms=20),
                         ['sheng-kbd=5', 'sheng-kbd-rails=1', 'sheng-kbd-khz=100', 'sheng-kbd-poll-read=20'])
        for bad in (dict(stage=0), dict(stage=6), dict(stage=1, khz=200), dict(stage=1, poll_read_ms=5)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                native_keyboard.boot_arguments(**bad)

    def test_packager_arguments(self):
        import package_macos26_ufs as packager
        common = ['--kernel-build', 'k', '--qemu-observation', 'q', '--bootstrap-observation', 'b',
                  '--default-off-observation', 'd', '--firmware-build', 'f', '--backup-dir', 'x', '--output', 'o',
                  '--root-deployment', 'a', '--partition-plan', 'b', '--root-build', 'c', '--root-hash', 'd',
                  '--root-volume-uuid', '808eac5e-8275-478d-814c-2229d8eb8d07']

        def run(*arguments):
            stderr = io.StringIO()
            with mock.patch.object(sys, 'argv', ['package', *common, *arguments]), contextlib.redirect_stderr(stderr), \
                    mock.patch.object(packager, 'qualify', side_effect=AssertionError('qualification reached')):
                with self.assertRaises((SystemExit, AssertionError)):
                    packager.main()
            return stderr.getvalue()

        self.assertIn('require --keyboard-stage', run('--keyboard-firmware', 'x'))
        self.assertIn('require --keyboard-stage', run('--keyboard-rails'))
        self.assertIn('give --keyboard-firmware', run('--keyboard-stage', '4'))
        self.assertIn('stage 3 or higher', run('--keyboard-stage', '2', '--keyboard-rails'))
        self.assertIn('invalid choice', run('--keyboard-stage', '6'))
        self.assertEqual(run('--keyboard-stage', '1'), '')   # reaches qualification

    def test_packager_requires_the_audited_companion(self):
        import json
        import package_macos26_ufs as packager
        with tempfile.TemporaryDirectory() as temporary:
            build = Path(temporary)

            def write(driver):
                (build / 'security-relink.json').write_text(json.dumps(dict(storage_driver=driver)))

            write(dict(build=dict(native_keyboard=native_keyboard.evidence()), keyboard_vtable_abi=dict(slots=334)))
            self.assertFalse(packager.qualify_keyboard(build)['runtime_qualified'])
            for driver in (dict(build={}, keyboard_vtable_abi={}), dict(build=dict(native_keyboard=native_keyboard.evidence()))):
                write(driver)
                with self.assertRaisesRegex(ValueError, 'audited keyboard'):
                    packager.qualify_keyboard(build)

    def test_storage_build_compiles_every_unit_and_declares_the_personalities(self):
        import build_macos26_storage
        import hashlib as digest

        with tempfile.TemporaryDirectory() as temporary:
            temp_root, out = Path(temporary) / 'root', Path(temporary) / 'out'
            out.mkdir()
            for rel in ('config/macos26-recovery-root.json', 'config/macoslog.json', 'platform/sheng/macos26/storage/macoslog_layout.h',
                        'platform/sheng/macos26/storage/macoslog_worker.h', 'platform/sheng/macos26/storage/recovery_root.h',
                        *[f'{native_keyboard.PLATFORM_DIR}/{name}' for name in native_keyboard.PLATFORM_SOURCES],
                        *[f'{native_keyboard.IMPORTED_DIR}/{name}' for name in (*native_keyboard.IMPORTED_C, *native_keyboard.IMPORTED_HEADERS,
                                                                                *native_keyboard.NAME_WRAPPERS)]):
                target = temp_root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / rel).read_bytes())
            for rel in ('UniXNU/drivers/NativeUfs/NativeUfs.cpp', 'UniXNU/drivers/NativeUfs/RootPublication.h',
                        'UniXNU/drivers/NativeUfs/RootWindowOverlay.h', 'UniXNU/drivers/NativeRamBlock/SparseDisk.h',
                        native_keyboard.UPSTREAM_HEADER):
                target = temp_root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('// stub\n')
            (temp_root / 'sources/macos26/IOStorageFamily').mkdir(parents=True)
            manifest = __import__('json').loads((ROOT / 'config/macos26-input-reference.json').read_text())
            siblings = out / 'ufs-driver/recovery-input/inputs'
            siblings.mkdir(parents=True)
            for name, entry in manifest['apple_headers']['files'].items():
                data = f'// {name}\n'.encode()
                entry['canonical_sha256'] = digest.sha256(data).hexdigest()
                (siblings / name).write_bytes(data)
            (temp_root / 'config/macos26-input-reference.json').write_text(__import__('json').dumps(manifest))
            sdk = Path('/sdk')
            commands = {}

            def record_run(command, label, record, cwd=None, env=None):
                commands[label] = list(command)
                if label.endswith('-link'):
                    Path(command[command.index('-o') + 1]).write_bytes(b'macho')

            compile_record = dict(directory=str(out), file='IOPlatformExpert.cpp',
                                  arguments=['clang++', '-xc++', '-fapple-kext', '-c', 'IOPlatformExpert.cpp', '-o', 'IOPlatformExpert.o'])
            record = {}
            with (mock.patch('prepare_macos26_gop.prepare', return_value={'source': 'NativeGOPDisplay.cpp'}),
                  mock.patch('subprocess.check_output', return_value=str(sdk) + '\n'),
                  mock.patch('build_macos26_storage.macho', return_value={'kind': 11, 'subtype': 0x40000002, 'commands': [(0x80000034, 0, 0)]})):
                with self.assertRaisesRegex(ValueError, 'UFS bundle'):
                    build_macos26_storage.build(temp_root, out, compile_record, out / 'source', record_run, {}, {}, gop_display=True, keyboard=True)
                build_macos26_storage.build(temp_root, out, compile_record, out / 'source', record_run, record, {},
                                            transport='ufs', gop_display=True, button_keys=False, keyboard=True)
            labels = [label for label in commands if '-keyboard-' in label]
            self.assertEqual(len(labels), 1 + len(native_keyboard.COMPILED_C), labels)
            cxx = next(commands[label] for label in labels if 'NativeKeyboard' in label)
            self.assertNotIn(str(sdk / 'usr/include'), cxx)
            self.assertIn(str(temp_root / 'UniXNU-sheng/package'), cxx)
            for label in labels:
                if 'NativeKeyboard' not in label:
                    self.assertIn('-xc', commands[label])
                    self.assertNotIn('-fapple-kext', commands[label])
            info = plistlib.loads((out / 'ufs-driver/Info.plist').read_bytes())
            self.assertEqual(info['IOKitPersonalities']['NativeKeyboard']['IOClass'], 'NativeKeyboard')
            self.assertEqual(info['IOKitPersonalities']['NativeKeyboardHIDEventDriver'], native_keyboard.EVENT_DRIVER)
            self.assertEqual(record['ufs_driver']['native_keyboard']['schema'], 'sheng-keyboard-v1')


if __name__ == '__main__':
    unittest.main()
