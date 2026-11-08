# SPDX-License-Identifier: GPL-3.0-or-later
"""Turnip macOS build: pins, patch repair, the 16 KiB patch, the cross file and the build-record contract. Needs no Mac tools or sources."""
import json
from pathlib import Path
import re
import struct
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import build_turnip_macos as turnip  # noqa: E402

OWN_PATCH = ROOT / turnip.OWN_PATCHES[0]


def record(**changes):
    good = dict(schema=turnip.SCHEMA, target=turnip.TARGET, abi_version=turnip.ABI_VERSION, hardware_qualified=False, runtime_tested=False,
                apple_content=False, vendor_firmware=False, upstream_patches=[{}] * 17, own_patches=[{}] * len(turnip.OWN_PATCHES),
                cdhash='a' * 40, foreign_libraries=[])
    return good | changes


class PinTests(unittest.TestCase):
    def test_every_source_is_a_full_commit(self):
        for name, entry in turnip.pins().items():
            self.assertRegex(entry['commit'], '^[0-9a-f]{40}$', name)

    def test_abi_header_is_the_one_the_kernel_build_uses(self):
        kernel = {entry['path']: entry['commit'] for entry in json.loads((ROOT / 'sources.lock.json').read_text())['sources']}
        self.assertEqual(turnip.pins()['Adreno740']['commit'], kernel['gpu/Adreno740'])


class PatchRepairTests(unittest.TestCase):
    NEW_FILE = 'diff --git a/x/new.h b/x/new.h\n--- /dev/null\n+++ b/x/new.h\n@@ -0,0 +1 @@\n+a\n'

    def test_missing_new_file_header_is_added(self):
        self.assertIn('new file mode 100644\n--- /dev/null', turnip.normalized_patch(self.NEW_FILE))

    def test_well_formed_patches_are_left_alone(self):
        complete = 'diff --git a/x/new.h b/x/new.h\nnew file mode 100644\nindex 000..111\n--- /dev/null\n+++ b/x/new.h\n'
        change = 'diff --git a/x/old.h b/x/old.h\n--- a/x/old.h\n+++ b/x/old.h\n@@ -1 +1 @@\n-a\n+b\n'
        self.assertEqual(turnip.normalized_patch(complete), complete)
        self.assertEqual(turnip.normalized_patch(change), change)


class PagePatchTests(unittest.TestCase):
    def setUp(self):
        self.lines = OWN_PATCH.read_text().splitlines()
        self.added = [line[1:] for line in self.lines if line.startswith('+') and not line.startswith('+++')]
        self.removed = [line[1:] for line in self.lines if line.startswith('-') and not line.startswith('---')]

    def test_touches_only_the_two_page_size_files(self):
        files = re.findall(r'^diff --git a/(\S+) ', OWN_PATCH.read_text(), re.M)
        self.assertEqual(files, ['src/freedreno/vulkan/tu_device.cc', 'src/freedreno/vulkan/tu_knl_darwin.cc'])

    def test_replaces_every_4k_assumption_with_the_cpu_page_size(self):
        self.assertEqual(sum('4095' in line for line in self.removed), 3)
        self.assertEqual(sum('= 4096' in line for line in self.removed), 1)
        self.assertFalse([line for line in self.added if '4095' in line or '= 4096' in line])
        self.assertEqual(sum('os_page_size' in line for line in self.added), 5)

    def test_reads_the_page_size_before_using_it(self):
        reading = next(i for i, line in enumerate(self.added) if 'os_get_page_size' in line)
        using = next(i for i, line in enumerate(self.added) if 'minImportedHostPointerAlignment' in line)
        self.assertLess(reading, using)


class BuildSettingsTests(unittest.TestCase):
    def test_cross_file_targets_arm64e_and_runs_nothing(self):
        tools = dict.fromkeys(('clang', 'clang++', 'ar', 'strip'), '/x/tool')
        text = turnip.cross_file_text(tools, ['-target', turnip.TARGET])
        self.assertIn("system = 'darwin'", text)
        self.assertIn('needs_exe_wrapper = true', text)
        self.assertEqual(text.count(repr(['-target', turnip.TARGET])), 6)
        self.assertEqual(turnip.TARGET, 'arm64e-apple-macos26.5')

    def test_options_build_only_the_darwin_backend(self):
        options = turnip.meson_options('/abi', 'a' * 64, 'b' * 40)
        for option in ('-Dvulkan-drivers=freedreno', '-Dfreedreno-kmds=darwin', '-Dtu-darwin-abi-dir=/abi', '-Dllvm=disabled', '-Dzstd=disabled',
                       '-Dbuildtype=release', f"-Dtu-builtin-source-id={'a' * 64}", f"-Dtu-build-id={'b' * 40}"):
            self.assertIn(option, options)

    def test_identities_have_the_lengths_the_options_need_and_follow_the_source(self):
        sources = dict(mesa_commit='c', source_diff_sha256='d', extra_sources={})
        first = turnip.identities(sources, 'clang')
        self.assertEqual([len(value) for value in first], [64, 40])
        self.assertNotEqual(first, turnip.identities(sources | dict(source_diff_sha256='e'), 'clang'))

    def test_only_thin_arm64e_passes(self):
        arm64e = struct.pack('<III', 0xfeedfacf, 0x0100000c, 2)
        self.assertTrue(turnip.is_arm64e(arm64e))
        self.assertFalse(turnip.is_arm64e(struct.pack('<III', 0xfeedfacf, 0x0100000c, 0)))
        self.assertFalse(turnip.is_arm64e(struct.pack('<III', 0xcafebabe, 0x0100000c, 2)))
        self.assertFalse(turnip.is_arm64e(b''))

    def test_homebrew_libraries_are_foreign(self):
        listing = ('lib.dylib:\n\t@rpath/lib.dylib (compatibility version 1.0.0)\n\t/usr/lib/libSystem.B.dylib (x)\n'
                   '\t/System/Library/Frameworks/IOKit.framework/Versions/A/IOKit (x)\n\t/opt/homebrew/lib/libz.dylib (x)\n')
        self.assertEqual(turnip.foreign_libraries(listing, '@rpath/lib.dylib'), ['/opt/homebrew/lib/libz.dylib'])


class ContractTests(unittest.TestCase):
    def test_good_record_passes(self):
        turnip.validate(record())

    def test_a_build_never_claims_more_than_compiling(self):
        for change in (dict(hardware_qualified=True), dict(runtime_tested=True), dict(apple_content=True), dict(vendor_firmware=True),
                       dict(abi_version=1), dict(target='arm64-apple-macos26.5'), dict(upstream_patches=[]), dict(own_patches=[]),
                       dict(cdhash='short'), dict(foreign_libraries=['/opt/homebrew/lib/libz.dylib'])):
            with self.assertRaises(SystemExit, msg=str(change)):
                turnip.validate(record(**change))


if __name__ == '__main__':
    unittest.main()
