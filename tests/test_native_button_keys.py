# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / 'platform/sheng/macos26/input'


class NativeButtonKeys(unittest.TestCase):
    def test_spmi_logic_against_a_simulated_arbiter(self):
        compiler = shutil.which('c++') or shutil.which('clang++')
        if compiler is None:
            self.skipTest('requires a host C++ compiler')
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / 'keys'
            subprocess.run([compiler, '-std=c++17', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined',
                            str(ROOT / 'tests/native_button_keys.cpp'), '-o', str(executable)],
                           check=True, capture_output=True, timeout=120)
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('PASS', result.stdout)

    def test_driver_is_default_off_read_only_and_hardware_only(self):
        source = (INPUT / 'native_button_keys.cpp').read_text()
        header = (INPUT / 'spmi_keys.h').read_text()
        self.assertIn('PE_parse_boot_argn("native-button-keys", &enabled, sizeof(enabled))', source)
        self.assertIn('enabled != 1', source)
        self.assertIn('PE_vmm_present != 0', source)
        # Only the observer (read) channel is ever written; no write channel, no PMIC write opcode.
        self.assertNotIn('chnls', source.lower().replace('no chnls', ''))
        self.assertEqual(re.findall(r'kOp\w+ = \d+', header), ['kOpExtReadLong = 1'])
        self.assertIn('writeObs(channel + kCmd, readCommand(address))', header)
        # The physical regions mapped are the arbiter's own (from the sheng DT), read-only where possible.
        for region in ('0x0c400000', '0x0c42d000', '0x0c440000'):
            self.assertIn(region, source)


if __name__ == '__main__':
    unittest.main()
