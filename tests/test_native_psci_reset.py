# SPDX-License-Identifier: GPL-3.0-or-later
"""PSCI reboot and power-off hook: default off, panic path untouched, applies with or without the RTC hook (no Apple content)."""
import contextlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import native_psci_reset  # noqa: E402

PATCHES = ROOT / 'platform/sheng/macos26/patches'


class PsciResetTests(unittest.TestCase):
    def setUp(self):
        patch = (ROOT / native_psci_reset.MACHINE_PATCH).read_text()
        self.added = '\n'.join(line[1:] for line in patch.splitlines() if line.startswith('+') and not line.startswith('+++'))

    def test_default_off_and_hardware_only(self):
        self.assertIn('PE_vmm_present == 0 && PE_parse_boot_argn ("sheng-psci-reset", &enabled, sizeof (enabled)) && enabled == 1', self.added)
        self.assertEqual(native_psci_reset.BOOT_ARGUMENT, 'sheng-psci-reset=1')

    def test_panic_types_are_not_handled(self):
        self.assertIn('if (type == kPERestartCPU) {', self.added)
        self.assertIn('type == kPEHaltCPU || type == kPEUPSDelayHaltCPU', self.added)
        self.assertNotIn('kPEPanicRestartCPU', self.added)

    def test_inherited_behaviour_runs_first_and_its_result_is_kept(self):
        self.assertLess(self.added.index('IODTPlatformExpert::haltRestart (type)'), self.added.index('platformPsci (0x84000009)'))
        self.assertIn('return result;', self.added)

    def test_applies_with_and_without_the_clock_hook(self):
        upstream = ROOT / 'UniXNU/drivers/ArmDTPlatform/ArmDTPlatform.cpp'
        if not upstream.exists():
            self.skipTest('upstream checkout not present')
        orders = (('machine-platform-reset.patch',),
                  ('machine-platform-time.patch', 'machine-platform-reset.patch'),
                  ('machine-platform-reset.patch', 'machine-platform-time.patch'))
        for order in orders:
            with tempfile.TemporaryDirectory() as temporary:
                tree = Path(temporary)
                (tree / 'ArmDTPlatform').mkdir()
                (tree / 'ArmDTPlatform/ArmDTPlatform.cpp').write_bytes(upstream.read_bytes())
                subprocess.run(['git', 'init', '-q'], cwd=tree, check=True)
                subprocess.run(['git', 'apply', '--include=ArmDTPlatform/*', str(PATCHES / 'machine-driver-declarations.patch')],
                               cwd=tree, check=True)
                for name in order:
                    subprocess.run(['git', 'apply', str(PATCHES / name)], cwd=tree, check=True)
                text = (tree / 'ArmDTPlatform/ArmDTPlatform.cpp').read_text()
                self.assertIn('int ArmDTPlatform::haltRestart (unsigned int type)', text)

    def test_packager_flag_needs_a_kernel_with_the_hook(self):
        import package_macos26_ufs as packager
        with tempfile.TemporaryDirectory() as temporary:
            build = Path(temporary)
            (build / 'build.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'PSCI reset hook'):
                packager.qualify_psci_reset(build)
            (build / 'build.json').write_text('{"platform_reset_patch_sha256": "%s"}' % native_psci_reset.patch_sha256())
            self.assertEqual(packager.qualify_psci_reset(build)['boot_argument'], 'sheng-psci-reset=1')

    def test_packager_flag_is_declared(self):
        import package_macos26_ufs as packager
        stderr = io.StringIO()
        with mock.patch.object(sys, 'argv', ['package', '--help']), contextlib.redirect_stdout(stderr), \
                self.assertRaises(SystemExit):
            packager.main()
        self.assertIn('--psci-reset', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
