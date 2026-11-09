# SPDX-License-Identifier: GPL-3.0-or-later
"""Memory performance reader: decoding logic, contract, read-only discipline, packager flag (no Apple content)."""
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
import native_memperf  # noqa: E402


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


class NativeMemPerfTests(unittest.TestCase):
    def test_decoding_against_synthetic_images(self):
        result = compile_and_run(ROOT / 'tests/native_memperf.cpp')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('PASS', result.stdout)

    def test_contract_is_default_off_and_read_only(self):
        record = native_memperf.evidence()
        native_memperf.validate(record)
        for change in (dict(default_off=False), dict(register_writes=True), dict(hardware_only=False),
                       dict(source_sha256=dict(record['source_sha256'], **{'platform/sheng/macos26/power/rpmh_readout.h': '0' * 64}))):
            with self.assertRaisesRegex(ValueError, 'default-off contract'):
                native_memperf.validate(record | change)

    def test_front_end_never_writes_a_register(self):
        directory = ROOT / native_memperf.PLATFORM_DIR
        front = (directory / 'native_memperf.cpp').read_text()
        header = (directory / 'rpmh_readout.h').read_text()
        self.assertIn('PE_parse_boot_argn("sheng-memperf", &mode, sizeof(mode)) || mode != 1', front)
        self.assertIn('PE_vmm_present != 0', front)
        self.assertEqual(re.findall(r'weak_import', front + header), [])
        # The register windows are opened for reading only and have no write path at all.
        window = front[front.index('struct Window'):front.index('uint32_t mibPerSecond')]
        self.assertIn('kIODirectionIn)', window)
        self.assertNotIn('write', window.lower())
        self.assertNotIn('->write', front)
        self.assertNotIn('void write', header)
        # Writes in the benchmark go only through the driver's own buffer mappings.
        self.assertEqual(front.count('p[i] ='), 1)
        for caller in re.findall(r'writeSpeed\(([^,]+),', front):
            self.assertRegex(caller, 'cached|device|fWriteCombined|volatile uint32_t \\*p')

    def test_packager_accepts_only_the_read_only_mode(self):
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

        self.assertIn('invalid choice', run('--memperf-mode', '2'))
        self.assertNotIn('invalid choice', run('--memperf-mode', '1'))


if __name__ == '__main__':
    unittest.main()
