# SPDX-License-Identifier: GPL-3.0-or-later
"""Wi-Fi companion: stage 1 and bring-up logic on the host, contract, upstream interface, packager argument (no Apple content, no firmware)."""
import contextlib
import io
import os
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
import native_wifi  # noqa: E402

NET = ROOT / native_wifi.PLATFORM_DIR
INPUT = ROOT / native_wifi.INPUT_DIR


class NativeWifiTests(unittest.TestCase):
    def run_fixture(self, name, extra=()):
        compiler = shutil.which('c++') or shutil.which('clang++')
        if compiler is None:
            self.skipTest('requires a host C++ compiler')
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / name
            subprocess.run([compiler, '-std=c++17', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined',
                            '-I', str(NET), '-I', str(INPUT), *extra, str(ROOT / 'tests' / f'{name}.cpp'), '-o', str(executable)],
                           check=True, capture_output=True, timeout=180)
            # The driver cannot unload: it never releases its mappings, by design, so leak reporting is off for this host run.
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=20,
                                    env=dict(os.environ, ASAN_OPTIONS='detect_leaks=0'))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('PASS', result.stdout)

    def test_power_snapshot_and_report_rows_against_a_simulated_arbiter(self):
        self.run_fixture('native_wifi_state')

    def test_front_end_gates_and_rows_over_stubbed_kernel_interfaces(self):
        self.run_fixture('native_wifi_frontend', ('-I', str(ROOT / 'tests/native_wifi_stubs'), '-Wno-unknown-warning-option', '-Wno-subobject-linkage'))

    def test_bring_up_order_and_failure_stops_against_a_simulated_soc(self):
        self.run_fixture('native_wifi_bringup')

    def contract_root(self, temporary) -> Path:
        root = Path(temporary)
        for rel in [Path(native_wifi.PLATFORM_DIR) / name for name in native_wifi.PLATFORM_SOURCES] + \
                [Path(native_wifi.INPUT_DIR) / name for name in native_wifi.INPUT_SOURCES]:
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / rel).read_bytes())
        upstream = root / native_wifi.UPSTREAM_CPP
        upstream.parent.mkdir(parents=True, exist_ok=True)
        upstream.write_text('// stub of the pinned upstream probe\n')
        return root

    def test_contract_is_default_off_and_read_only_at_stage_one(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.contract_root(temporary)
            record = native_wifi.evidence(root)
            native_wifi.validate(record, root)
            for change in (dict(default_off=False), dict(read_only_stage=2), dict(storage_writes=True), dict(firmware_compiled_in=True),
                           dict(source_sha256=dict(record['source_sha256'], **{native_wifi.UPSTREAM_CPP: '0' * 64}))):
                with self.assertRaisesRegex(ValueError, 'default-off contract'):
                    native_wifi.validate(record | change, root)

    def test_front_end_matches_the_upstream_probe_interface(self):
        probe = ROOT / native_wifi.UPSTREAM_CPP
        if not probe.exists():
            self.skipTest('upstream checkout not present')
        probe_text = probe.read_text()
        front = (NET / 'native_wifi.cpp').read_text()
        self.assertIn('volatile uint32_t native_wifi_stats[10];', probe_text)
        self.assertIn('extern volatile uint32_t native_wifi_stats[10];', front)
        self.assertIn('extern "C" void sheng_wifi_probe(void)', probe_text)
        self.assertIn('void sheng_wifi_probe(void);', front)
        self.assertIn('PE_parse_boot_argn("sheng-wifi"', probe_text)
        self.assertEqual(re.findall(r'weak_import', probe_text), [])

    def test_stage_gates_in_the_front_end(self):
        front = (NET / 'native_wifi.cpp').read_text()
        self.assertIn('PE_parse_boot_argn("sheng-wifi", &stage, sizeof(stage))', front)
        self.assertIn('stage < 1 || stage > kMaxStage', front)
        self.assertIn('PE_vmm_present != 0', front)
        self.assertIn('if (wifi->fStage >= 2)', front)
        self.assertEqual(front.count('sheng_wcn::bringUp('), 1)
        for text in (front, (NET / 'wcn_state.h').read_text(), (NET / 'wcn_bringup.h').read_text(), (NET / 'wcn_rpmh.h').read_text()):
            self.assertEqual(re.findall(r'weak_import', text), [])
        # Stage 1 reaches the arbiter only through spmi_keys.h, whose only opcode is the read.
        header = (INPUT / 'spmi_keys.h').read_text()
        self.assertEqual(re.findall(r'kOp\w+ = \d+', header), ['kOpExtReadLong = 1'])
        self.assertIn('spmiWrite8', (NET / 'wcn_bringup.h').read_text())
        self.assertNotIn('spmiWrite8', (NET / 'wcn_state.h').read_text())

    def test_report_rows_stay_inside_the_table(self):
        front = (NET / 'native_wifi.cpp').read_text()
        self.assertIn('kRowUart = kRowSleepClock + 1, kRows = kRowUart + 1', front)
        body = front[front.index('void NativeWifi::collect('):front.index('void NativeWifi::runBringUp()')]
        body = body[body.index('{'):]
        indices = set(re.findall(r'rows\[(\w+(?: \+ \w+)?)\]', body))
        self.assertEqual(indices, {'kRowClocks', 'kRowLink', 'kRowPins', 'kRowPins + 1', 'kRowRails + i', 'kRowSleepClock', 'kRowUart'})
        self.assertNotIn('row++', body)

    def test_imported_files_keep_their_license(self):
        for name in ('wcn_bringup.h', 'wcn_rpmh.h', 'wcn_pcie_tables.h'):
            first = (NET / name).read_text().splitlines()[0]
            self.assertIn('BSD-2-Clause-Patent', first, name)
        for name in ('wcn_bringup.h', 'wcn_rpmh.h'):
            self.assertIn(native_wifi.UPSTREAM_DERIVED_FROM, (NET / name).read_text(), name)

    def test_nothing_vendor_is_built_in(self):
        for name in native_wifi.PLATFORM_SOURCES:
            text = (NET / name).read_text()
            self.assertNotIn('amss', text.lower(), name)
            self.assertNotIn('board-2', text.lower(), name)
            self.assertLess(len(text), 200000, name)

    def test_packager_stage_argument(self):
        try:
            import package_macos26_ufs as packager
        except ModuleNotFoundError as error:
            if error.name != 'native_prelink':
                raise
            self.skipTest('native_prelink is provided by the CI checkout')
        common =['--kernel-build', 'k', '--qemu-observation', 'q', '--bootstrap-observation', 'b',
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

        self.assertIn('invalid choice', run('--wifi-stage', '4'))
        self.assertIn('invalid choice', run('--wifi-stage', '0'))
        for stage in ('1', '2', '3'):
            self.assertNotIn('invalid choice', run('--wifi-stage', stage))

    def test_kernel_build_option(self):
        import build_macos26_storage
        with self.assertRaisesRegex(ValueError, 'Wi-Fi companion belongs to the hardware'):
            build_macos26_storage.build(Path('.'), Path('.'), {}, None, None, None, None, transport='virtio', wifi=True)


if __name__ == '__main__':
    unittest.main()
