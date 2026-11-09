# SPDX-License-Identifier: GPL-3.0-or-later
"""Panel brightness companion: register logic, contract, front-end discipline, packager flags (no Apple content)."""
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
import native_backlight  # noqa: E402


def compile_and_run(source):
    compiler = shutil.which('c++') or shutil.which('clang++')
    if compiler is None:
        raise unittest.SkipTest('requires a host C++ compiler')
    with tempfile.TemporaryDirectory() as temporary:
        executable = Path(temporary) / 'test'
        base = [compiler, '-std=c++17', '-Wall', '-Wextra', '-Werror', str(source), '-o', str(executable)]
        built = subprocess.run([*base, '-fsanitize=address,undefined'], capture_output=True, timeout=180)
        if built.returncode != 0:
            subprocess.run(base, check=True, capture_output=True, timeout=180)  # no sanitizer runtime on this host
        return subprocess.run([str(executable)], capture_output=True, text=True, timeout=20)


class NativeBacklightTests(unittest.TestCase):
    def setUp(self):
        directory = ROOT / native_backlight.PLATFORM_DIR
        self.front = (directory / 'native_backlight.cpp').read_text()
        self.header = (directory / 'ktz8866_i2c.h').read_text()

    def test_register_logic_against_a_simulated_chip(self):
        result = compile_and_run(ROOT / 'tests/native_backlight.cpp')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('PASS', result.stdout)

    def test_contract_is_default_off_and_keeps_the_panel_visible(self):
        record = native_backlight.evidence()
        native_backlight.validate(record)
        for change in (dict(default_off=False), dict(read_only_stage=2), dict(hardware_only=False), dict(min_visible_level=0),
                       dict(source_sha256=dict(record['source_sha256'], **{'platform/sheng/macos26/power/ktz8866_i2c.h': '0' * 64}))):
            with self.assertRaisesRegex(ValueError, 'default-off contract'):
                native_backlight.validate(record | change)

    def test_clamp_constants_match_the_contract(self):
        self.assertIn(f'kMaxLevel = {native_backlight.MAX_LEVEL}', self.header)
        self.assertIn(f'kMinVisibleLevel = {native_backlight.MIN_VISIBLE_LEVEL}', self.header)

    def test_front_end_is_default_off_and_hardware_only(self):
        self.assertIn('PE_parse_boot_argn("sheng-backlight", &stage, sizeof(stage)) || stage < 1 || stage > 4', self.front)
        self.assertIn('PE_vmm_present != 0', self.front)
        self.assertEqual(re.findall(r'weak_import', self.front + self.header), [])

    def test_stage_one_stops_before_any_engine_write(self):
        stage_one = self.front[self.front.index('uint32_t summary[2]'):self.front.index('if (stage == 1) {')]
        self.assertNotIn('prepare', stage_one)
        self.assertNotIn('readLevel', stage_one)
        before_stage_one = self.front[self.front.index('bool NativeBacklight::start'):self.front.index('if (stage == 1) {')]
        self.assertNotIn('ensureClocks()', before_stage_one.replace('stage >= 2 && !running && !ensureClocks()', ''))

    def test_only_the_level_registers_are_ever_written(self):
        # 0x08 is the chip's enable register; bias and configuration registers must not appear either.
        self.assertEqual(re.findall(r'kBrt\w+ = 0x0[45]', self.header), ['kBrtLsb = 0x04', 'kBrtMsb = 0x05'])
        self.assertNotIn('0x08', self.header)
        # Every write of a level goes through the clamp in setBoth.
        self.assertEqual(self.front.count('setBoth('), 1)
        self.assertEqual(self.front.count('writeLevel('), 0)
        self.assertIn('level = clampLevel(level);', self.header)

    def test_boot_arguments(self):
        self.assertEqual(native_backlight.boot_arguments(1), 'sheng-backlight=1')
        self.assertEqual(native_backlight.boot_arguments(3, 512), 'sheng-backlight=3 sheng-backlight-level=512')
        with self.assertRaisesRegex(ValueError, 'needs stage 3'):
            native_backlight.boot_arguments(2, 512)
        with self.assertRaisesRegex(ValueError, 'within'):
            native_backlight.boot_arguments(3, 10)

    def test_packager_rejects_unknown_stages(self):
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

        self.assertIn('invalid choice', run('--backlight-stage', '5'))
        self.assertNotIn('invalid choice', run('--backlight-stage', '1'))


if __name__ == '__main__':
    unittest.main()
