# SPDX-License-Identifier: GPL-3.0-or-later
"""QEMU-only GPU trust-path probe: contract, CDHash, payload, boot policy and trust entry (synthetic data, no Apple content)."""
import hashlib
from pathlib import Path
import plistlib
import re
import struct
import sys
import tempfile
import unittest
import uuid

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
