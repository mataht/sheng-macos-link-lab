# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'platform/sheng/macos26/patches/0068-gpu-probe-exit-trace.patch'


class NativeExitTrace(unittest.TestCase):
    def test_real_parser_handles_good_and_hostile_images(self):
        compiler = shutil.which('cc')
        if compiler is None:
            self.skipTest('requires a host C compiler')
        source = Path(__file__).with_name('native_exit_trace.c')
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / 'exit-trace'
            subprocess.run([compiler, '-std=c11', '-D_DEFAULT_SOURCE', '-Wall', '-Wextra', '-Werror',
                            '-fsanitize=address,undefined', '-fno-sanitize-recover=all', str(source), '-o', str(executable)],
                           check=True, capture_output=True, timeout=30)
            subprocess.run([str(executable)], check=True, capture_output=True, timeout=20)

    def test_patch_only_adds_and_is_listed(self):
        text = PATCH.read_text()
        removed = [line for line in text.splitlines() if line.startswith('-') and not line.startswith('---')]
        self.assertEqual(removed, [])
        build = (ROOT / 'scripts/build_macos26_xnu.py').read_text()
        self.assertIn('0068-gpu-probe-exit-trace.patch', build)
        self.assertIn("bsd/kern/sheng_exit_trace.h", build)

    def test_trace_is_default_off_and_limited_to_windowserver(self):
        added = '\n'.join(line[1:] for line in PATCH.read_text().splitlines() if line.startswith('+') and not line.startswith('+++'))
        self.assertIn('PE_parse_boot_argn("sheng-exit-trace", &enabled, sizeof(enabled))', added)
        self.assertIn('enabled != 1', added)
        self.assertIn('"WindowServer"', added)
        self.assertIn('p != current_proc()', added)


if __name__ == '__main__':
    unittest.main()
