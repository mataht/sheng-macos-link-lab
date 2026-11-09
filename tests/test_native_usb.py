# SPDX-License-Identifier: GPL-3.0-or-later
"""USB companion: contract, host logic against a simulated SoC, packager argument (no Apple content)."""
import contextlib
import io
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import native_usb  # noqa: E402

USB = ROOT / native_usb.PLATFORM_DIR


class NativeUsbTests(unittest.TestCase):
    def compile_and_run(self, source, *extra):
        compiler = shutil.which('c++') or shutil.which('clang++')
        if compiler is None:
            self.skipTest('requires a host C++ compiler')
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / 'test'
            for sanitize in (['-fsanitize=address,undefined'], []):
                build = subprocess.run([compiler, '-std=c++17', '-Wall', '-Wextra', '-Werror', *sanitize, *extra, str(source), '-o', str(executable)],
                                       capture_output=True, text=True, timeout=120)
                if build.returncode == 0:
                    break
            else:
                self.fail(build.stdout + build.stderr)
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('PASS', result.stdout)

    def test_register_logic_against_a_simulated_soc(self):
        self.compile_and_run(ROOT / 'tests/native_usb.cpp')

    def test_real_driver_compiles_against_stub_iokit_headers(self):
        compiler = shutil.which('c++') or shutil.which('clang++')
        if compiler is None:
            self.skipTest('requires a host C++ compiler')
        with tempfile.TemporaryDirectory() as temporary:
            include = Path(temporary) / 'include'
            shutil.copytree(ROOT / 'tests/native_usb_stubs', include)
            for name in native_usb.PLATFORM_SOURCES:
                shutil.copy(USB / name, include / name)
            shutil.copy(ROOT / native_usb.ALSO_COPIED[0], include / 'spmi_keys.h')
            subprocess.run([compiler, '-std=gnu++17', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter', '-Wno-unused-function',
                            '-I', str(include), '-fsyntax-only', str(include / 'native_usb.cpp')],
                           check=True, capture_output=True, text=True, timeout=120)
            # The imported C file compiles on its own.
            subprocess.run([compiler.replace('++', '') if shutil.which(compiler.replace('++', '')) else 'cc', '-std=gnu11', '-Wall', '-Werror',
                            '-I', str(include), '-c', str(include / 'native_usb_rpmh.c'), '-o', str(Path(temporary) / 'rpmh.o')],
                           check=True, capture_output=True, text=True, timeout=120)

    def test_contract_is_default_off_and_hardware_only(self):
        record = native_usb.evidence()
        native_usb.validate(record)
        self.assertEqual(record['stage_argument'], 'native-usb')
        self.assertIn('1', record['stages'])
        for change in (dict(default_off=False), dict(hardware_only=False), dict(storage_writes=True), dict(dma=True),
                       dict(source_sha256=dict(record['source_sha256'], **{native_usb.PLATFORM_DIR + '/native_usb.cpp': '0' * 64}))):
            with self.assertRaisesRegex(ValueError, 'default-off contract'):
                native_usb.validate(record | change)

    def test_driver_source_keeps_its_safety_properties(self):
        source = (USB / 'native_usb.cpp').read_text()
        header = (USB / 'usb_hw.h').read_text()
        self.assertIn('PE_parse_boot_argn("native-usb"', source)
        self.assertIn('PE_vmm_present != 0', source)
        self.assertEqual(re.findall(r'weak_import', source + header), [])
        # Stage 1 reaches the controller only through the power/clock gate.
        self.assertIn('if (!coreAccessible(p))', header)
        # Up to stage 2 the driver never writes the PMIC: the SPMI access is the observer read path.
        self.assertNotIn('repeaterWrite', source)
        self.assertNotIn('kSpmiChn +', source)
        # No ring, doorbell or run bit is set anywhere.
        self.assertNotIn('kUsbCmdRun)', header.replace('kUsbCmdRun, 0)', ''))
        # Nothing licensed GPL-2.0-only is in this directory, and the Linux-derived PHY tables are not imported.
        for path in USB.iterdir():
            self.assertNotRegex(path.read_text(), r'SPDX-License-Identifier: GPL-2\.0', path.name)
        for name in ('QcomEusb2Phy.h', 'QcomQmpUsb3.h'):
            self.assertFalse((USB / name).exists())

    def test_packager_accepts_only_known_stages(self):
        import package_macos26_ufs as packager
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

        self.assertIn('invalid choice', run('--usb-stage', '3'))
        self.assertNotIn('invalid choice', run('--usb-stage', '1'))
        self.assertNotIn('invalid choice', run('--usb-stage', '2'))


if __name__ == '__main__':
    unittest.main()
