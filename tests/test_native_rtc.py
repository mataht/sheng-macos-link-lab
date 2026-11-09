# SPDX-License-Identifier: GPL-3.0-or-later
"""PMIC RTC companion: register logic, contract, front-end discipline, platform hook, packager flags (no Apple content)."""
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
import native_rtc  # noqa: E402


def compile_and_run(source, *includes):
    compiler = shutil.which('c++') or shutil.which('clang++')
    if compiler is None:
        raise unittest.SkipTest('requires a host C++ compiler')
    with tempfile.TemporaryDirectory() as temporary:
        executable = Path(temporary) / 'test'
        base = [compiler, '-std=c++17', '-Wall', '-Wextra', '-Werror', *includes, str(source), '-o', str(executable)]
        built = subprocess.run([*base, '-fsanitize=address,undefined'], capture_output=True, timeout=180)
        if built.returncode != 0:
            subprocess.run(base, check=True, capture_output=True, timeout=180)  # no sanitizer runtime on this host
        return subprocess.run([str(executable)], capture_output=True, text=True, timeout=20)


class NativeRtcTests(unittest.TestCase):
    def test_register_logic_against_a_simulated_arbiter(self):
        result = compile_and_run(ROOT / 'tests/native_rtc.cpp', '-I', str(ROOT / 'platform/sheng/macos26/input'))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('PASS', result.stdout)

    def test_contract_is_default_off_and_read_only(self):
        record = native_rtc.evidence()
        native_rtc.validate(record)
        for change in (dict(default_off=False), dict(read_only_mode=2), dict(pmic_writes=True), dict(hardware_only=False),
                       dict(source_sha256=dict(record['source_sha256'], **{native_rtc.MACHINE_PATCH: '0' * 64}))):
            with self.assertRaisesRegex(ValueError, 'default-off contract'):
                native_rtc.validate(record | change)

    def test_front_end_is_default_off_hardware_only_and_never_writes_the_pmic(self):
        front = (ROOT / native_rtc.PLATFORM_DIR / 'native_rtc.cpp').read_text()
        header = (ROOT / native_rtc.PLATFORM_DIR / 'native_rtc.h').read_text()
        self.assertIn('PE_parse_boot_argn("sheng-rtc", &mode, sizeof(mode)) || mode < 1 || mode > 2', front)
        self.assertIn('PE_vmm_present != 0', front)
        self.assertEqual(re.findall(r'weak_import', front + header), [])
        # Only the observer channel; no arbiter write channel (0xc500000), no PMIC write opcode.
        self.assertNotIn('0x0c500000', front)
        self.assertNotIn('kOpExtWrite', front + header)
        # IORTC and the calendar change only in mode 2, and the time is stored before the resource appears.
        branch = front[front.index('if (mode == 2) {'):front.index('} else {\n    IOLog("NativeRtc: read-only')]
        self.assertLess(branch.index('sheng_platform_rtc_seconds'), branch.index('publishResource("IORTC")'))
        self.assertEqual(front.count('publishResource('), 1)

    def test_platform_hook_only_reads_the_handed_over_time(self):
        patch = (ROOT / native_rtc.MACHINE_PATCH).read_text()
        added = '\n'.join(line[1:] for line in patch.splitlines() if line.startswith('+') and not line.startswith('+++'))
        self.assertIn('long getGMTTimeOfDay (void) override;', added)
        self.assertIn('volatile long sheng_platform_rtc_seconds = 0;', added)
        self.assertIn('if (base <= 0) {', added)
        self.assertNotIn('setGMTTimeOfDay', added)  # the OS setting the clock stays the platform expert's no-op
        self.assertNotIn('(*', added)  # data crosses the module boundary, not a function pointer

    def test_platform_hook_applies_to_the_pinned_upstream_file(self):
        upstream = ROOT / 'UniXNU/drivers/ArmDTPlatform/ArmDTPlatform.cpp'
        if not upstream.exists():
            self.skipTest('upstream checkout not present')
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary)
            (tree / 'ArmDTPlatform').mkdir()
            (tree / 'ArmDTPlatform/ArmDTPlatform.cpp').write_bytes(upstream.read_bytes())
            subprocess.run(['git', 'init', '-q'], cwd=tree, check=True)
            for patch in ('machine-driver-declarations.patch', 'machine-platform-time.patch'):
                include = [] if patch == 'machine-platform-time.patch' else ['--include=ArmDTPlatform/*']
                subprocess.run(['git', 'apply', *include, str(ROOT / 'platform/sheng/macos26/patches' / patch)],
                               cwd=tree, check=True)

    def test_boot_arguments_fit_the_kernel_parser(self):
        self.assertEqual(native_rtc.boot_arguments(1), 'sheng-rtc=1')
        self.assertEqual(native_rtc.boot_arguments(2, -7, 1794180000), 'sheng-rtc=2 sheng-rtc-offset=-7 sheng-rtc-floor=1794180000')
        with self.assertRaisesRegex(ValueError, 'does not fit'):
            native_rtc.boot_arguments(2, floor=2 ** 31)
        with self.assertRaisesRegex(ValueError, 'positive'):
            native_rtc.boot_arguments(2, floor=0)

    def test_packager_rejects_unknown_modes(self):
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

        self.assertIn('invalid choice', run('--rtc-mode', '3'))
        self.assertNotIn('invalid choice', run('--rtc-mode', '2', '--rtc-floor', '1794180000'))


if __name__ == '__main__':
    unittest.main()
