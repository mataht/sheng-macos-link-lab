# SPDX-License-Identifier: GPL-3.0-or-later
"""Host checks for the native-gop-diag strip and the bounded first-frame framebuffer claim."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))


class NativeGopDiag(unittest.TestCase):
    def compile_and_run(self, source):
        compiler = shutil.which('c++') or shutil.which('g++') or shutil.which('clang++')
        if compiler is None:
            self.skipTest('requires a host C++ compiler')
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / 'test'
            subprocess.run([compiler, '-std=c++17', '-Wall', '-Wextra', '-Werror',
                            '-fsanitize=address,undefined', str(source), '-o', str(executable)],
                           check=True, capture_output=True, timeout=60)
            subprocess.run([str(executable)], check=True, capture_output=True, timeout=30)

    def test_strip_rendering(self):
        self.compile_and_run(Path(__file__).with_name('native_gop_diag.cpp'))

    def test_bounded_claim(self):
        self.compile_and_run(Path(__file__).with_name('native_framebuffer_claim.cpp'))

    def test_capture_handoff_option_removes_only_that_argument(self):
        import package_macos26_ufs as packager
        args = 'a -sheng-early-console -sheng-capture-handoff native-ufs-root=1'
        self.assertEqual(packager.without_capture_handoff(args), 'a -sheng-early-console native-ufs-root=1')
        self.assertEqual(packager.without_capture_handoff('x'), 'x')


if __name__ == '__main__':
    unittest.main()
