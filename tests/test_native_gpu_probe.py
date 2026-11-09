# SPDX-License-Identifier: GPL-3.0-or-later
"""QEMU-only GPU trust-path probe: contract, CDHash, payload, boot policy and trust entry (synthetic data, no Apple content)."""
import hashlib
import os
from pathlib import Path
import plistlib
import re
import struct
import sys
import tempfile
import unittest
import uuid
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import native_gpu_probe as probe  # noqa: E402
import system_trust_cache as trust  # noqa: E402
from native_devtree import configure_native_text_state  # noqa: E402

PLUGIN = dict(plugin_sha256='a' * 64, plugin_bytes=1, plugin_cdhash='b' * 40, plugin_cd_hash_type=2, info_plist_sha256='c' * 64)


def signed_macho(hash_type=2, magic=0xfade0c02, code=b'code'):
    directory = struct.pack('>IIIIIIIIIBBBB', magic, 44 + len(code), 0x20400, 0, 44, 44, 0, 0, 0, 32, hash_type, 0, 14) + bytes(4) + code
    superblob = struct.pack('>IIIII', 0xfade0cc0, 20 + len(directory), 1, 0, 20) + directory
    command = struct.pack('<IIII', 0x1d, 16, 512, len(superblob))
    header = struct.pack('<IIIIIIII', 0xfeedfacf, 0x0100000c, 0x80000002, 8, 1, len(command), 0, 0)
    body = header + command
    return body + bytes(512 - len(body)) + superblob, directory


def node(name, properties=(), children=()):
    props = (('name', name.encode() + b'\0'), *properties)
    return (struct.pack('<II', len(props), len(children))
            + b''.join(struct.pack('<32sI', key.encode(), len(value)) + value + bytes(-len(value) % 4)
                       for key, value in props) + b''.join(children))


class ContractTests(unittest.TestCase):
    def test_contract_is_qemu_only_and_default_off(self):
        record = probe.evidence(ROOT, PLUGIN)
        probe.validate(record)
        for change in (dict(default_off=False), dict(qemu_only=False), dict(hardware_admitted=True), dict(gpu_acceleration=True),
                       dict(plugin_cd_hash_type=1), dict(plugin_cdhash='ab'), dict(plugin_class='Other'),
                       dict(boot_argument='sheng-gpu-probe=2'),
                       dict(source_sha256=dict(record['source_sha256'], **{probe.PLUGIN_SOURCE: '0' * 64}))):
            with self.assertRaisesRegex(ValueError, 'QEMU-only default-off contract'):
                probe.validate(record | change)

    def test_personality_names_the_plugin_class_metal_will_load(self):
        self.assertEqual(probe.PERSONALITY['IOMatchCategory'], 'IOAccelerator')
        self.assertEqual(probe.PERSONALITY['MetalPluginName'], probe.PLUGIN_NAME)
        self.assertEqual(probe.PERSONALITY['MetalPluginClassName'], probe.PLUGIN_CLASS)
        plist = plistlib.loads(probe.info_plist())
        self.assertEqual((plist['CFBundleExecutable'], plist['NSPrincipalClass']), (probe.PLUGIN_NAME, probe.PLUGIN_CLASS))
        plugin = (ROOT / probe.PLATFORM_DIR / probe.PLUGIN_SOURCE).read_text()
        self.assertIn(f'@interface {probe.PLUGIN_CLASS} : IOGPUMetalDevice', plugin)
        self.assertIn('initWithAcceleratorPort', plugin)
        self.assertIn('IOGPUMetalDevice', (ROOT / probe.PLATFORM_DIR / probe.LINK_STUB).read_text())

    def test_driver_is_off_unless_asked_and_refused_on_hardware(self):
        source = (ROOT / probe.PLATFORM_DIR / probe.DRIVER_SOURCE).read_text()
        self.assertIn('PE_parse_boot_argn("sheng-gpu-probe"', source)
        self.assertIn('PE_vmm_present == 0', source)
        self.assertIn('public IOAccelerator', source)
        self.assertIn('registerService()', source)
        self.assertEqual(re.findall(r'weak_import', source), [])

    def test_probe_only_in_the_virtio_bundle(self):
        import build_macos26_storage as storage
        with self.assertRaisesRegex(ValueError, 'QEMU-only VirtIO'):
            storage.build(ROOT, ROOT, {}, ROOT, None, {}, {}, transport='ufs', gop_display=True, gpu_probe=True)
        with self.assertRaisesRegex(ValueError, 'QEMU-only VirtIO'):
            storage.build(ROOT, ROOT, {}, ROOT, None, {}, {}, gop_display=False, gpu_probe=True)

    def test_kernel_patch_is_listed_and_adds_only_the_helper(self):
        patch = (ROOT / 'platform/sheng/macos26/patches/0067-gpu-probe-kernel-mkdir.patch').read_text()
        added = [line for line in patch.splitlines() if line.startswith('+') and not line.startswith('+++')]
        removed = [line for line in patch.splitlines() if line.startswith('-') and not line.startswith('---')]
        self.assertEqual(removed, [])
        self.assertTrue(any('sheng_kernel_mkdir(const char *path, int mode)' in line for line in added))
        self.assertIn('0067-gpu-probe-kernel-mkdir.patch', (ROOT / 'scripts/build_macos26_xnu.py').read_text())


class RealPluginTests(unittest.TestCase):
    def bundle(self, directory):
        contents = Path(directory) / 'NativeAdrenoMetal.bundle/Contents'
        (contents / 'MacOS').mkdir(parents=True)
        (contents / 'Info.plist').write_bytes(b'plist')
        plugin, _ = signed_macho(code=b'p')
        turnip, _ = signed_macho(code=b'turnip')
        (contents / 'MacOS/NativeAdrenoMetal').write_bytes(plugin)
        (contents / 'MacOS/libvulkan_freedreno.dylib').write_bytes(turnip)
        return contents.parent, plugin, turnip

    def test_node_carries_the_three_files_and_both_cdhashes_are_named(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle, plugin, turnip = self.bundle(temporary)
            properties = probe.real_plugin_node(bundle)
            for key, expected in (('info-plist', b'plist'), ('executable', plugin), ('turnip', turnip)):
                self.assertEqual(struct.unpack_from('<Q', properties[key])[0], len(expected))
                self.assertEqual(zlib.decompress(properties[key][8:]), expected)
            self.assertEqual(probe.real_plugin_cdhashes(bundle), (probe.cdhash(plugin)[0].hex(), probe.cdhash(turnip)[0].hex()))

    def test_a_bundle_too_big_for_the_loader_is_refused_here(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle, _, _ = self.bundle(temporary)
            (bundle / 'Contents/MacOS/NativeAdrenoMetal').write_bytes(os.urandom(probe.DEVICE_TREE_ROOM + 1024))
            with self.assertRaisesRegex(ValueError, 'device tree limit'):
                probe.real_plugin_node(bundle)

    def test_node_is_added_once_and_only_to_a_development_tree(self):
        tree = node('device-tree', children=(node('chosen'), node('arm-io')))
        properties = {'compatible': b'x\0', 'info-plist': b'i', 'executable': b'e' * 40, 'turnip': b't'}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'kernel.adt'
            path.write_bytes(tree)
            configure_native_text_state(path, development_root=True, gpu_plugin_node=properties)
            self.assertEqual(path.read_bytes().count(b'native-gpu-plugin\0'), 1)
            self.assertIn(b'e' * 40, path.read_bytes())
            path.write_bytes(tree)
            with self.assertRaisesRegex(ValueError, 'development QEMU tree'):
                configure_native_text_state(path, gpu_plugin_node=properties)
            with self.assertRaisesRegex(ValueError, 'development QEMU tree'):
                configure_native_text_state(path, development_root=True, gpu_plugin_node={'compatible': b'x\0'})

    def test_driver_names_the_real_plugin_only_when_the_node_exists(self):
        source = (ROOT / probe.PLATFORM_DIR / probe.DRIVER_SOURCE).read_text()
        self.assertIn('"/arm-io/native-gpu-plugin"', source)
        self.assertIn('setProperty("MetalPluginName", REAL_NAME)', source)
        self.assertIn('if (real) {', source)
        self.assertIn('libvulkan_freedreno.dylib', source)

    def test_real_plugin_is_never_in_the_hardware_bundle_or_the_build(self):
        self.assertNotIn('real_plugin', (ROOT / 'scripts/build_macos26_storage.py').read_text())
        self.assertNotIn('real_plugin', (ROOT / 'scripts/package_macos26_ufs.py').read_text())

    def test_trust_tool_accepts_both_cdhashes(self):
        entries = trust.merge([struct.pack('<I', 2) + bytes(16) + struct.pack('<I', 0)], (b'\x01' * 20, b'\x02' * 20))
        self.assertEqual(entries[1]['owner_entries'], 2)


class CdHashTests(unittest.TestCase):
    def test_cdhash_is_the_truncated_digest_of_the_code_directory(self):
        data, directory = signed_macho()
        digest, kind = probe.cdhash(data)
        self.assertEqual(digest, hashlib.sha256(directory).digest()[:20])
        self.assertEqual(kind, 2)

    def test_cdhash_refuses_other_files(self):
        with self.assertRaisesRegex(ValueError, 'SHA-256 CodeDirectory'):
            probe.cdhash(signed_macho(hash_type=1)[0])
        with self.assertRaisesRegex(ValueError, 'SHA-256 CodeDirectory'):
            probe.cdhash(signed_macho(magic=0xfade0c01)[0])
        with self.assertRaisesRegex(ValueError, 'thin arm64e'):
            probe.cdhash(bytes(64))

    def test_payload_header_embeds_both_files(self):
        text = probe.payload_header(b'\x01\x02', bytes(range(40)))
        self.assertIn('#define SHENG_GPU_PROBE_NAME "ShengProbeMetal"', text)
        self.assertRegex(text, r'gShengGpuProbeInfoPlist\[\] = \{\n1,2\n\}')
        values = re.search(r'gShengGpuProbeExecutable\[\] = \{\n(.*?)\n\}', text, re.S).group(1).replace('\n', '')
        self.assertEqual(values, ','.join(str(i) for i in range(40)))


class TrustEntryTests(unittest.TestCase):
    @staticmethod
    def module(hashes):
        return struct.pack('<I', 2) + b'U' * 16 + struct.pack('<I', len(hashes)) + b''.join(h + bytes([2, 0, 0, 0]) for h in sorted(hashes))

    def test_owner_entry_is_merged_in_order_with_apple_entries_unchanged(self):
        low, high, own = b'\x01' * 20, b'\x09' * 20, b'\x05' * 20
        module, info = trust.merge([self.module([low, high])], (own,))
        _, entries = trust.module_entries(module)
        self.assertEqual([e[:20] for e in entries], [low, own, high])
        self.assertEqual(entries[1][20:], bytes([2, 0, 0, 0]))
        self.assertEqual(info['owner_entries'], 1)
        trust.check_bundle(trust.bundle(module))

    def test_owner_entry_must_be_new_and_well_formed(self):
        low = b'\x01' * 20
        for value in (low, b'short'):
            with self.assertRaisesRegex(ValueError, 'owner CDHash'):
                trust.merge([self.module([low])], (value,))


class TrustAdmissionTests(unittest.TestCase):
    def record(self, bundle, owner=()):
        record = dict(schema='sheng-system-trust-cache-v1', build='25F71', bundle_sha256=hashlib.sha256(bundle).hexdigest(),
                      own_entries_added=bool(owner))
        if owner:
            record['owner_cdhashes'] = list(owner)
        return record

    def test_normal_boots_still_refuse_any_added_entry(self):
        self.assertTrue(trust.admit_record(self.record(b'x'), b'x'))
        self.assertFalse(trust.admit_record(self.record(b'x', ('a' * 40,)), b'x'))
        self.assertFalse(trust.admit_record(self.record(b'x') | dict(own_entries_added=True), b'x'))
        self.assertFalse(trust.admit_record(self.record(b'x'), b'y'))

    def test_probe_boot_allows_only_the_declared_plugin(self):
        wanted = ('a' * 40,)
        self.assertTrue(trust.admit_record(self.record(b'x', wanted), b'x', wanted))
        self.assertFalse(trust.admit_record(self.record(b'x', ('b' * 40,)), b'x', wanted))
        self.assertFalse(trust.admit_record(self.record(b'x', ('a' * 40, 'b' * 40)), b'x', wanted))
        self.assertFalse(trust.admit_record(self.record(b'x'), b'x', wanted))


class BootPolicyTests(unittest.TestCase):
    def test_unrestricted_fs_only_for_a_full_system_experiment(self):
        group = uuid.UUID('6a2c7f1e-1111-4222-8333-944455556666')
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'adt'
            original = node('device-tree', children=(node('chosen'),))
            path.write_bytes(original)
            with self.assertRaisesRegex(ValueError, 'full-system experiment'):
                configure_native_text_state(path, development_root=True, full_system_lp_sip0=2)
            self.assertEqual(path.read_bytes(), original)
            configure_native_text_state(path, development_root=True, full_system_volume_group=group, full_system_lp_sip0=2)
            data = path.read_bytes()
            at = data.index(b'lp-sip0')
            self.assertEqual(data[at + 32 + 4:at + 32 + 4 + 8], struct.pack('<Q', 2))


if __name__ == '__main__':
    unittest.main()
