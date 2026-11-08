# SPDX-License-Identifier: GPL-3.0-or-later
"""CPU frequency companion: contract, front-end/probe interface, packager argument (no Apple content)."""
import contextlib
import io
from pathlib import Path
import re
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import native_cpufreq  # noqa: E402


class NativeCpuFreqTests(unittest.TestCase):
    def test_contract_is_default_off_read_only_mode_one(self):
        record = native_cpufreq.evidence()
        native_cpufreq.validate(record)
        for change in (dict(default_off=False), dict(read_only_mode=2), dict(storage_writes=True),
                       dict(source_sha256=dict(record['source_sha256'], **{native_cpufreq.UPSTREAM_CPP: '0' * 64}))):
            with self.assertRaisesRegex(ValueError, 'default-off contract'):
                native_cpufreq.validate(record | change)

    def test_front_end_matches_the_upstream_probe_interface(self):
        probe = (ROOT / native_cpufreq.UPSTREAM_CPP)
        if not probe.exists():
            self.skipTest('upstream checkout not present')
        probe_text = probe.read_text()
        front = (ROOT / native_cpufreq.PLATFORM_DIR / 'native_cpufreq.cpp').read_text()
        self.assertIn('char native_cpufreq_row[120];', probe_text)
        self.assertIn('extern char native_cpufreq_row[120];', front)
        for name in ('sheng_cpufreq_probe', 'sheng_cpufreq_poll'):
            self.assertIn(f'extern "C" void {name}(void)', probe_text)
            self.assertIn(f'void {name}(void);', front)
        self.assertIn('PE_parse_boot_argn("sheng-cpufreq"', probe_text)
        self.assertIn('PE_vmm_present != 0', front)
        self.assertEqual(re.findall(r'weak_import', probe_text + front), [])

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

        self.assertIn('invalid choice', run('--cpufreq-mode', '2'))
        self.assertNotIn('invalid choice', run('--cpufreq-mode', '1'))


if __name__ == '__main__':
    unittest.main()
