# SPDX-License-Identifier: GPL-3.0-or-later
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
DISPLAY = ROOT / 'platform/sheng/macos26/display'
PATCH = DISPLAY / '0001-native-gop-display.patch'
MARKER = '+// Explicit display-activity policy, default off.'


def added_block(patch: str, first: str) -> str:
    """Return the contiguous added lines that start with `first`, without their '+' prefix."""
    lines = patch.splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.startswith(first))
    block = []
    for line in lines[start:]:
        if not line.startswith('+'):
            break
        block.append(line[1:])
    return ''.join(block)


class NativeGopKeepAwake(unittest.TestCase):
    def compiler(self):
        # Same preference as test_native_gop_scanout: the host c++ normally ships the sanitizer runtimes.
        compiler = shutil.which('c++') or shutil.which('clang++')
        if compiler is None:
            self.skipTest('requires a host C++ compiler')
        return compiler

    def test_policy_helpers_from_the_patch(self):
        compiler = self.compiler()
        helpers = added_block(PATCH.read_text(), MARKER)
        self.assertIn('static bool nativeGopHardwareKeepAwakeAllowed(', helpers)
        self.assertIn('static void nativeGopVMKeepAwakeTick(', helpers)
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            (work / 'helpers.inc').write_text(helpers)
            executable = work / 'keep-awake'
            subprocess.run([compiler, '-std=c++17', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function',
                            '-fsanitize=address,undefined', '-DKEEP_AWAKE_HELPERS="' + str(work / 'helpers.inc') + '"',
                            str(ROOT / 'tests/native_gop_keep_awake.cpp'), '-o', str(executable)],
                           check=True, capture_output=True, timeout=60)
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('PASS', result.stdout)

    def test_start_gates_and_record(self):
        patch = PATCH.read_text()
        added = ''.join(line[1:] for line in patch.splitlines(keepends=True)
                        if line.startswith('+') and not line.startswith('+++'))
        # The Stage18B VM gate is kept verbatim and runs before the hardware gate.
        vm_gate = ('    if (vmKeepAwake > 1 || (vmKeepAwake == 1 && PE_vmm_present != 1)) {\n'
                   '        IOLog("NativeGOPDisplay: VM keep-awake requires explicit VMM opt-in\\n");\n'
                   '        return false;\n'
                   '    }\n')
        hardware_gate = ('    unsigned hwKeepAwake = 0;\n'
                         '    PE_parse_boot_argn("native-gop-keep-awake", &hwKeepAwake, sizeof(hwKeepAwake));\n'
                         '    if (!nativeGopHardwareKeepAwakeAllowed(hwKeepAwake, vmKeepAwake, PE_vmm_present)) {\n')
        self.assertEqual(added.count(vm_gate + hardware_gate), 1)
        self.assertEqual(added.count(
            '    if ((vmKeepAwake == 1 || hwKeepAwake == 1) && !nativeGopStartVMKeepAwake(this, hwKeepAwake == 1)) {\n'), 1)
        self.assertEqual(added.count('    nativeGopVMKeepAwakeTick(this);\n'), 1)
        # Observer tooling reads these file-local symbols by name; they must keep their Stage18B names.
        for symbol in ('static IOService *gVMKeepAwakeOwner = nullptr;\n',
                       'static uint64_t gVMKeepAwakeDeadline = UINT64_MAX;\n'):
            self.assertEqual(added.count(symbol), 1)
        record = json.loads((DISPLAY / 'hw-keep-awake.json').read_text())
        current = hashlib.sha256(PATCH.read_bytes()).hexdigest()
        panel_path = DISPLAY / 'hw-panel-size.json'
        if record['patch_sha256'] != current:
            # Only the reviewed default-off panel-size step, optionally followed by the always-on copy-forward step,
            # may follow the keep-awake patch.
            panel = json.loads(panel_path.read_text())
            self.assertEqual(panel['schema'], 'sheng-hw-display-panel-size-v1')
            self.assertEqual(panel['base_patch_sha256'], record['patch_sha256'])
            self.assertTrue(panel['default_off'])
            if panel['patch_sha256'] != current:
                forward = json.loads((DISPLAY / 'hw-copy-forward.json').read_text())
                self.assertEqual(forward['schema'], 'sheng-hw-display-copy-forward-v1')
                self.assertEqual(forward['base_patch_sha256'], panel['patch_sha256'])
                self.assertEqual(forward['patch_sha256'], current)
                self.assertEqual(forward['scanout_sha256'], record['scanout_sha256'])
        self.assertEqual(record['scanout_sha256'], hashlib.sha256((DISPLAY / 'gop_scanout.h').read_bytes()).hexdigest())
        self.assertEqual(record['hardware_switch']['boot_argument'], 'native-gop-keep-awake=1')
        self.assertFalse(record['compiled'] or record['hardware_run'] or record['ci_submitted'])

    def test_row_blit_and_source_staging_with_the_scanout_header(self):
        compiler = self.compiler()
        header = str(DISPLAY / 'gop_scanout.h')
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            for name, extra, argument in (('native_gop_blit.cpp', [], None),
                                          ('source_lease_regression.cpp', ['-DGOP_HEADER="' + header + '"'],
                                           'expect-pass')):
                executable = work / name.removesuffix('.cpp')
                sanitizers = ['-fsanitize=address,undefined'] if argument is None else []
                subprocess.run([compiler, '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', *sanitizers, *extra,
                                str(ROOT / 'tests' / name), '-o', str(executable)],
                               check=True, capture_output=True, timeout=60)
                result = subprocess.run([str(executable)] + ([argument] if argument else []),
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, name + ': ' + result.stderr)


if __name__ == '__main__':
    unittest.main()
