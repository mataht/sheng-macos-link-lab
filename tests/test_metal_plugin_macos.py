# SPDX-License-Identifier: GPL-3.0-or-later
"""Metal plugin assembly: pins, the bundle description, the commands and the build-record contract. Needs no Mac tools or sources."""
import json
from pathlib import Path
import plistlib
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import build_metal_plugin_macos as plugin  # noqa: E402


def record(**changes):
    part = dict(cdhash='a' * 40, foreign_libraries=[])
    good = dict(schema=plugin.SCHEMA, target=plugin.TARGET, runtime_tested=False, hardware_qualified=False, apple_content=False, vendor_firmware=False, release=False,
                plugin=part, turnip=part, principal_class_defined=True)
    return good | changes


class PinTests(unittest.TestCase):
    def test_every_source_is_a_full_commit(self):
        for name, entry in plugin.pins().items():
            self.assertRegex(entry['commit'], '^[0-9a-f]{40}$', name)

    def test_frontend_is_the_revision_the_bridge_was_built_from(self):
        bridge = {Path(entry['path']).name: entry['commit'] for entry in json.loads((ROOT / 'config/metal-bridge-sources.json').read_text())['sources']}
        self.assertEqual(plugin.pins()['AdrenoMetal']['commit'], bridge['AdrenoMetal'])

    def test_only_the_plugin_directory_of_droidvm_is_fetched(self):
        self.assertEqual(plugin.pins()['macOS-DroidVM']['sparse'], ['overlay/metal'])


class BundleTests(unittest.TestCase):
    def test_info_plist_names_the_class_metal_loads(self):
        info = plistlib.loads(plugin.info_plist())
        self.assertEqual(info['NSPrincipalClass'], 'NativeAdrenoMetalDevice')
        self.assertEqual(info['CFBundleExecutable'], 'NativeAdrenoMetal')
        self.assertEqual(info['CFBundlePackageType'], 'BNDL')

    def test_commands_target_arm64e_and_use_the_stub(self):
        compiled = plugin.compile_command('c++', '/sdk', '/inc', 'a.mm', 'a.o')
        linked = plugin.link_command('c++', '/sdk', ['a.o'], 'lib.a', 'IOGPU.tbd', 'out')
        for command in (compiled, linked):
            self.assertIn('arm64e-apple-macos26.5', command)
        self.assertIn('-fobjc-arc', compiled)
        for item in ('-bundle', '-Wl,-dead_strip', 'lib.a', 'IOGPU.tbd', 'Metal', '-liconv'):
            self.assertIn(item, linked)

    def test_the_stub_declares_only_the_base_class(self):
        text = (ROOT / plugin.LINK_STUB).read_text()
        self.assertIn('IOGPUMetalDevice', text)
        self.assertEqual(text.count('objc-classes'), 1)
        self.assertNotIn('symbols:', text)


class ReadoutTests(unittest.TestCase):
    def test_homebrew_libraries_are_foreign(self):
        listing = 'x:\n\t/usr/lib/libSystem.B.dylib (x)\n\t/System/Library/PrivateFrameworks/IOGPU.framework/Versions/A/IOGPU (x)\n\t/opt/homebrew/lib/libz.dylib (x)\n\t@rpath/own.dylib (x)\n'
        self.assertEqual(plugin.foreign_libraries(listing, ('@rpath/own.dylib',)), ['/opt/homebrew/lib/libz.dylib'])

    def test_cdhash_comes_from_codesign(self):
        self.assertEqual(plugin.describe_signature('Page size=4096\nCDHash=' + 'b' * 40 + '\n'), 'b' * 40)
        with self.assertRaises(SystemExit):
            plugin.describe_signature('no hash')


class ContractTests(unittest.TestCase):
    def test_good_record_passes(self):
        plugin.validate(record())

    def test_a_build_never_claims_more_than_linking(self):
        for change in (dict(runtime_tested=True), dict(hardware_qualified=True), dict(apple_content=True), dict(vendor_firmware=True), dict(release=True), dict(target='arm64-apple-macos26.5')):
            with self.assertRaises(SystemExit, msg=str(change)):
                plugin.validate(record(**change))

    def test_each_mach_o_needs_its_cdhash_and_system_libraries_only(self):
        for part in ('plugin', 'turnip'):
            for bad in (dict(cdhash='short', foreign_libraries=[]), dict(cdhash='a' * 40, foreign_libraries=['/opt/homebrew/lib/x.dylib'])):
                with self.assertRaises(SystemExit, msg=part):
                    plugin.validate(record(**{part: bad}))

    def test_the_principal_class_must_be_there(self):
        with self.assertRaises(SystemExit):
            plugin.validate(record(principal_class_defined=False))


if __name__ == '__main__':
    unittest.main()
