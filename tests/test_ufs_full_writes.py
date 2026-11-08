# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise the full-system root persistent write path against an immutable disk seed."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class FullRootWrites(unittest.TestCase):
    def test_batched_writes_read_modify_write_and_fail_stop(self):
        compiler = shutil.which('clang++') or shutil.which('g++')
        if not compiler:
            self.skipTest('requires a host C++ compiler')
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / 'writes'
            warnings = ['-Wunneeded-internal-declaration'] if 'clang' in Path(compiler).name else []
            build = subprocess.run([compiler, *warnings, '-std=c++17', '-Wall', '-Wextra', '-Werror',
                                    '-fsanitize=address,undefined', str(ROOT / 'tests/ufs_full_writes.cpp'),
                                    '-o', str(executable)], capture_output=True, text=True, timeout=60)
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run([str(executable)], capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stderr)


if __name__ == '__main__':
    unittest.main()
