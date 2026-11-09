# SPDX-License-Identifier: GPL-3.0-or-later
"""Metal bridge macOS build: pins, the target spec, the crate manifest, the environment, library inspection and the build-record contract. Needs no Mac tools or sources."""
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import build_metal_bridge_macos as bridge  # noqa: E402

PAC_CENSUS = dict(authenticated_calls=10, pacibsp=20, paciza=30)


def record(**changes):
    good = dict(schema=bridge.SCHEMA, target=bridge.TARGET, abi=bridge.ABI, toolchain=bridge.TOOLCHAIN, hardware_qualified=False, runtime_tested=False, apple_content=False,
                vendor_firmware=False, patches=[{}] * bridge.PATCH_COUNT, non_arm64e_members=[], census=PAC_CENSUS, missing_exports=[], link_check=dict(ok=True, foreign_libraries=[]))
    return good | changes


def object_file(words, relocation_types=(), cpu_subtype=0x80000002, flags=0x80000400):
    code = b''.join(struct.pack('<I', word) for word in words)
    start = 32 + 72 + 80
    relocations = b''.join(struct.pack('<II', 0, kind << 28) for kind in relocation_types)
    header = struct.pack('<8I', 0xfeedfacf, 0x0100000c, cpu_subtype, 1, 1, 72 + 80, 0, 0)
    segment = struct.pack('<II16sQQQQIIII', 0x19, 72 + 80, b'', 0, len(code), start, len(code), 7, 7, 1, 0)
    section = struct.pack('<16s16sQQIIIIIIII', b'__text', b'__TEXT', 0, len(code), start, 2, start + len(code), len(relocation_types), flags, 0, 0, 0)
    return header + segment + section + code + relocations


def archive(*members):
    data = b'!<arch>\n'
    for name, body in members:
        encoded = name.encode()
        padded = encoded + b'\0' * (-len(encoded) % 8)
        content = padded + body
        size = len(content)
        data += b'%-16s%-12s%-6s%-6s%-8s%-10d`\n' % (b'#1/%d' % len(padded), b'0', b'0', b'0', b'644', size) + content
        if size & 1:
            data += b'\n'
    return data


class PinTests(unittest.TestCase):
    def test_every_source_is_a_full_commit(self):
        for name, entry in bridge.pins().items():
            self.assertRegex(entry['commit'], '^[0-9a-f]{40}$', name)

    def test_same_unixnu_as_the_turnip_build(self):
        turnip = json.loads((ROOT / 'config/turnip-sources.json').read_text())['sources']
        self.assertEqual(bridge.pins()['UniXNU']['commit'], next(entry['commit'] for entry in turnip if entry['path'].endswith('UniXNU')))

    def test_workflow_installs_the_toolchain_the_script_needs(self):
        workflow = (ROOT / '.github/workflows/gpu-userland.yml').read_text()
        self.assertIn(f'toolchain install {bridge.TOOLCHAIN} ', workflow)
        self.assertIn('--component rust-src', workflow)


class TargetSpecTests(unittest.TestCase):
    BUILTIN = json.dumps({'arch': 'aarch64', 'llvm-target': 'arm64e-apple-macosx', 'is-builtin': True, 'os': 'macos'})

    def test_selects_the_clang_abi(self):
        spec = bridge.target_spec(self.BUILTIN)
        self.assertEqual((spec['abi'], spec['llvm-abiname']), ('pauthtest', 'pauthtest'))

    def test_changes_nothing_else(self):
        spec = bridge.target_spec(self.BUILTIN)
        self.assertEqual({key: spec[key] for key in ('arch', 'llvm-target', 'os')}, {'arch': 'aarch64', 'llvm-target': 'arm64e-apple-macosx', 'os': 'macos'})
        self.assertNotIn('is-builtin', spec)


class BuildSettingsTests(unittest.TestCase):
    def test_manifest_is_one_lto_unit_of_the_two_runtime_crates(self):
        text = bridge.manifest_text('/m', '/t')
        for line in ('crate-type = ["staticlib"]', 'path = "/m/bridge/NativeMetalBridge.rs"', 'metal2vulkan = { path = "/t/crates/metal2vulkan", features = ["ash"] }',
                     'metal2vulkan-runtime = { path = "/t/crates/metal2vulkan-runtime" }', 'lto = "fat"', 'codegen-units = 1', 'panic = "unwind"', '[workspace]'):
            self.assertIn(line, text)

    def test_environment_replaces_inherited_rust_settings(self):
        tools = dict(clang='/c', ar='/a')
        tools['clang++'] = '/cxx'
        env = bridge.build_env({'RUSTFLAGS': '-Cbad', 'RUSTC_WRAPPER': 'x', 'HOME': '/h'}, tools, '/sdk', dict(NMB_FIXTURE_AIR='/f.air'), 'id')
        self.assertEqual(env['RUSTFLAGS'], bridge.RUSTFLAGS)
        self.assertNotIn('RUSTC_WRAPPER', env)
        self.assertEqual((env['HOME'], env['NMB_RUNTIME_ID'], env['NMB_FIXTURE_AIR']), ('/h', 'id', '/f.air'))
        self.assertEqual(env['CRATE_CC_NO_DEFAULTS'], '1')
        self.assertEqual(env['CFLAGS_arm64e_apple_darwin'], '-target arm64e-apple-macos26.5 -isysroot /sdk -O2')

    def test_all_three_embedded_shaders_are_assembled(self):
        inputs = bridge.air_inputs('/t', '/m')
        self.assertEqual(sorted(inputs), ['NMB_FC_AIR', 'NMB_FIXTURE_AIR', 'NMB_SCANOUT_AIR'])
        self.assertEqual(inputs['NMB_SCANOUT_AIR'].as_posix(), '/m/shaders/scanout_convert.ll')

    def test_runtime_id_follows_the_patched_tree(self):
        first = bridge.runtime_id('c', 'tree1')
        self.assertEqual(len(first), 32)
        self.assertNotEqual(first, bridge.runtime_id('c', 'tree2'))

    def test_header_exports_lists_functions_only(self):
        self.assertEqual(bridge.header_exports('uint32_t nmb_a(int);\nstruct NmbSelftest *nmb_b_c (void);\n#define NMB_X 1\n'), {'nmb_a', 'nmb_b_c'})


class ArchiveTests(unittest.TestCase):
    def test_reads_long_names_and_skips_symbol_tables(self):
        members = bridge.archive_members(archive(('__.SYMDEF SORTED', b'abc'), ('one.rcgu.o', b'body1'), ('two.o', b'bo')))
        self.assertEqual(members, [('one.rcgu.o', b'body1'), ('two.o', b'bo')])

    def test_refuses_something_else(self):
        with self.assertRaises(SystemExit):
            bridge.archive_members(b'\xcf\xfa\xed\xfe')


class InspectionTests(unittest.TestCase):
    WORDS = (0xd63f0200,  # blr x16
             0xd63f0a1f,  # blraaz x16
             0xd73f0a11,  # blraa x16, x17
             0xd61f0200,  # br x16
             0xd65f0fff,  # retab
             0xd65f03c0,  # ret
             0xd503237f,  # pacibsp
             0xd503201f,  # nop, which has the same bits as pacibsp outside the hint field
             0xdac123f0)  # paciza x16

    def test_census_tells_the_encodings_apart(self):
        counts = bridge.census(struct.pack('<9I', *self.WORDS))
        for name, expected in dict(blr=1, blraaz=1, blraa=1, br=1, retab=1, ret=1, pacibsp=1, paciza=1, braaz=0, autibsp=0).items():
            self.assertEqual(counts[name], expected, name)

    def test_census_ignores_a_partial_trailing_word(self):
        self.assertEqual(bridge.census(struct.pack('<I', 0xd63f0a1f) + b'\x01')['blraaz'], 1)

    def test_library_totals_and_signed_relocations(self):
        data = archive(('a.o', object_file(self.WORDS, (11, 11, 0))), ('b.o', object_file(self.WORDS, (11,))))
        result = bridge.inspect_library(data)
        self.assertEqual(result['census']['authenticated_calls'], 4)
        self.assertEqual(result['census']['auth_relocations'], 3)
        self.assertEqual(result['non_arm64e_members'], [])
        self.assertEqual(result['members'][0]['cpu_subtype'], '0x80000002')

    def test_data_sections_are_not_counted_as_code(self):
        result = bridge.inspect_library(archive(('a.o', object_file(self.WORDS, flags=0))))
        self.assertEqual(result['census']['authenticated_calls'], 0)

    def test_plain_arm64_objects_are_reported(self):
        result = bridge.inspect_library(archive(('a.o', object_file(self.WORDS, cpu_subtype=0))))
        self.assertEqual(result['non_arm64e_members'], ['a.o'])

    def test_symbols_come_from_the_object_itself(self):
        names = b'\0_nmb_a\0_nmb_undefined\0_private\0'
        start = 32 + 24
        table = struct.pack('<IBBHQ', 1, 0x0f, 1, 0, 0) + struct.pack('<IBBHQ', 8, 0x01, 0, 0, 0) + struct.pack('<IBBHQ', 23, 0x0e, 1, 0, 0)
        header = struct.pack('<8I', 0xfeedfacf, 0x0100000c, 0x80000002, 1, 1, 24, 0, 0)
        symtab = struct.pack('<6I', 2, 24, start, 3, start + len(table), len(names))
        self.assertEqual(bridge.defined_symbols(header + symtab + table + names), {'_nmb_a'})

    def test_homebrew_libraries_are_foreign(self):
        listing = 'x:\n\t/usr/lib/libSystem.B.dylib (x)\n\t/System/Library/Frameworks/Metal.framework/Metal (x)\n\t/opt/homebrew/lib/libz.dylib (x)\n'
        self.assertEqual(bridge.foreign_libraries(listing), ['/opt/homebrew/lib/libz.dylib'])


class PrepareTests(unittest.TestCase):
    def test_refuses_a_translator_at_the_wrong_commit(self):
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for name in ('translator', 'metal', 'unixnu'):
                (base / name).mkdir()
                subprocess.run(['git', '-C', str(base / name), 'init', '-q'], check=True)
                subprocess.run(['git', '-C', str(base / name), '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', 'commit', '-q', '--allow-empty', '-m', 'x'], check=True)
            (base / 'unixnu/scripts/data').mkdir(parents=True)
            (base / 'unixnu/scripts/data/gpu-metal-vulkan-inputs.json').write_text(json.dumps(dict(translator=dict(commit='0' * 40, files={}))))
            with self.assertRaises(SystemExit):
                bridge.prepare(base / 'translator', base / 'metal', base / 'unixnu')


class ContractTests(unittest.TestCase):
    def test_good_record_passes(self):
        bridge.validate(record())

    def test_a_build_never_claims_more_than_compiling(self):
        for change in (dict(hardware_qualified=True), dict(runtime_tested=True), dict(apple_content=True), dict(vendor_firmware=True), dict(abi=''), dict(target='aarch64-apple-darwin'),
                       dict(toolchain='nightly'), dict(patches=[])):
            with self.assertRaises(SystemExit, msg=str(change)):
                bridge.validate(record(**change))

    def test_pointer_authentication_must_be_visible_in_the_code(self):
        for key in PAC_CENSUS:
            with self.assertRaises(SystemExit, msg=key):
                bridge.validate(record(census=PAC_CENSUS | {key: 0}))

    def test_refuses_objects_of_another_architecture_and_missing_functions(self):
        for change in (dict(non_arm64e_members=['x.o']), dict(missing_exports=['nmb_a'])):
            with self.assertRaises(SystemExit, msg=str(change)):
                bridge.validate(record(**change))

    def test_the_library_must_link(self):
        for link in (dict(ok=False), dict(ok=True, foreign_libraries=['/opt/homebrew/lib/libz.dylib']), {}):
            with self.assertRaises(SystemExit, msg=str(link)):
                bridge.validate(record(link_check=link))


if __name__ == '__main__':
    unittest.main()
