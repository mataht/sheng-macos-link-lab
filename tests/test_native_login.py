# SPDX-License-Identifier: GPL-3.0-or-later
"""Login-path services: contract, default-off gating, host logic, build/packager wiring (no Apple content)."""
import contextlib
import io
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
LOGIN = ROOT / 'platform/sheng/macos26/login'
sys.path.insert(0, str(ROOT / 'scripts'))
import native_login  # noqa: E402


class NativeLoginTests(unittest.TestCase):
    def test_host_logic(self):
        compiler = shutil.which('c++') or shutil.which('clang++')
        if compiler is None:
            self.skipTest('requires a host C++ compiler')
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / 'login'
            subprocess.run([compiler, '-std=c++17', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined',
                            str(ROOT / 'tests/native_login_host.cpp'), '-o', str(executable)],
                           check=True, capture_output=True, timeout=180)
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('PASS', result.stdout)

    def test_driver_type_checks_against_stubs(self):
        compiler = shutil.which('c++') or shutil.which('clang++')
        if compiler is None:
            self.skipTest('requires a host C++ compiler')
        subprocess.run([compiler, '-std=c++17', '-fsyntax-only', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
                        '-I', str(ROOT / 'tests/login_stubs'), '-I', str(LOGIN), str(LOGIN / 'native_login_services.cpp')],
                       check=True, capture_output=True, timeout=120)

    def test_contract_is_default_off(self):
        record = native_login.evidence()
        native_login.validate(record)
        for change in (dict(default_off=False), dict(storage_writes=True), dict(schema='x'),
                       dict(source_sha256=dict(record['source_sha256'], **{'platform/sheng/macos26/login/aks_protocol.h': '0' * 64}))):
            with self.assertRaisesRegex(ValueError, 'default-off contract'):
                native_login.validate(record | change)

    def test_services_are_boot_argument_gated_and_use_the_apple_names(self):
        source = (LOGIN / 'native_login_services.cpp').read_text()
        self.assertIn('PE_parse_boot_argn("sheng-aks", &value, sizeof(value))', source)
        self.assertIn('PE_parse_boot_argn("sheng-watchdog", &enabled, sizeof(enabled))', source)
        self.assertNotIn('PE_vmm_present', re.sub(r'//.*', '', source))  # the gate is the boot argument: QEMU is meant to run them
        self.assertIn('if (aks_stage() == 0 || !IOService::start(provider)) return false;', source)
        # libaks opens IOService:/IOResources/AppleKeyStore; the registry name is the class name, never setName()d.
        self.assertIn('class AppleKeyStore final : public IOService', source)
        self.assertIn('class AppleKeyStoreUserClient final : public IOUserClient', source)
        self.assertIn('class IOWatchdog final : public IOService', source)
        self.assertIn('class IOWatchdogUserClient final : public IOUserClient', source)
        self.assertNotIn('setName(', source)
        self.assertEqual(re.findall(r'weak_import', source), [])
        names = re.findall(r'^\s+(\w+)=dict\(IOClass', (ROOT / 'scripts/native_login.py').read_text(), re.M)
        self.assertEqual(sorted(native_login.PERSONALITIES), ['AppleKeyStore', 'IOWatchdog'])
        for item in native_login.PERSONALITIES.values():
            self.assertEqual(item['IOProviderClass'], 'IOResources')

    def test_personality_audit(self):
        bundle = 'org.example.bundle'
        record = native_login.evidence()
        info = dict(IOKitPersonalities=native_login.personalities(bundle))
        self.assertEqual(native_login.validate_bundle(record, info, bundle), {'AppleKeyStore', 'IOWatchdog'})
        info['IOKitPersonalities']['IOWatchdog'] = dict(info['IOKitPersonalities']['IOWatchdog'], IOProbeScore=1)
        with self.assertRaisesRegex(ValueError, 'differs'):
            native_login.validate_bundle(record, info, bundle)

    def test_build_and_packager_wiring(self):
        import build_macos26_storage
        import package_macos26_ufs as packager
        self.assertIn('login', build_macos26_storage.build.__kwdefaults__)
        self.assertIs(build_macos26_storage.build.__kwdefaults__['login'], False)
        common = ['--kernel-build', 'k', '--qemu-observation', 'q', '--bootstrap-observation', 'b',
                  '--default-off-observation', 'd', '--firmware-build', 'f', '--backup-dir', 'x', '--output', 'o',
                  '--root-deployment', 'a', '--partition-plan', 'b', '--root-build', 'c', '--root-hash', 'd',
                  '--root-volume-uuid', '808eac5e-8275-478d-814c-2229d8eb8d07']

        def run(*arguments):
            stderr = io.StringIO()
            with mock.patch.object(sys, 'argv', ['package', *common, *arguments]), contextlib.redirect_stderr(stderr), \
                    mock.patch.object(packager, 'qualify', side_effect=AssertionError('qualification reached')):
                with self.assertRaises((SystemExit, AssertionError, OSError, ValueError)):
                    packager.main()
            return stderr.getvalue()

        self.assertIn('invalid choice', run('--aks-mode', '3'))
        self.assertNotIn('invalid choice', run('--aks-mode', '1'))
        self.assertNotIn('unrecognized', run('--aks-mode', '2', '--native-watchdog'))


if __name__ == '__main__':
    unittest.main()
