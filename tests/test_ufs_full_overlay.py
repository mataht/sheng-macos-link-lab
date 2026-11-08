# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise the full-system root overlay (two-level, volatile) against an immutable disk seed."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class FullRootOverlay(unittest.TestCase):
    def test_batched_reads_copy_on_write_limits_and_release(self):
        compiler = shutil.which('clang++') or shutil.which('g++')
        if not compiler:
            self.skipTest('requires a host C++ compiler')
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / 'overlay'
            warnings = ['-Wunneeded-internal-declaration'] if 'clang' in Path(compiler).name else []
            build = subprocess.run([compiler, *warnings, '-std=c++17', '-Wall', '-Wextra', '-Werror',
                                    '-fsanitize=address,undefined', str(ROOT / 'tests/ufs_full_overlay.cpp'),
                                    '-o', str(executable)], capture_output=True, text=True, timeout=60)
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run([str(executable)], capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stderr)


if __name__ == '__main__':
    unittest.main()
