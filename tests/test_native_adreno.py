# SPDX-License-Identifier: GPL-3.0-or-later
"""Adreno 740 kernel stack: default-off contract, firmware pins, the 16 KiB patch and the board patch (no Apple or vendor content)."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import native_adreno as adreno  # noqa: E402

CPU_PATCH = ROOT / adreno.PATCHES[0]
BOARD_PATCH = ROOT / adreno.PATCHES[1]


def added(path):
    return [line[1:] for line in path.read_text().splitlines() if line.startswith('+') and not line.startswith('+++')]


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.build_identifier = adreno.build_identifier
        adreno.build_identifier = lambda root=ROOT: '0' * 16
        self.addCleanup(setattr, adreno, 'build_identifier', self.build_identifier)

    def test_contract_is_default_off_and_keeps_firmware_out_of_builds(self):
        record = adreno.evidence()
        adreno.validate(record)
        for change in (dict(default_off=False), dict(firmware_compiled_in=True), dict(hardware_only=False),
                       dict(vm_refused=False), dict(hardware_qualified=True), dict(mode_argument='other'),
                       dict(firmware=[]),
                       dict(source_sha256=dict(record['source_sha256'], **{adreno.INPUTS: '0' * 64}))):
            with self.assertRaisesRegex(ValueError, 'default-off contract'):
                adreno.validate(record | change)

    def test_personalities_publish_only_through_the_driver_service(self):
        self.assertEqual(adreno.ACCELERATOR_PERSONALITY['IOProviderClass'], 'NativeAdrenoGpuPublished')
        self.assertEqual(adreno.ACCELERATOR_PERSONALITY['IOMatchCategory'], 'IOAccelerator')
        self.assertEqual(adreno.ACCELERATOR_PERSONALITY['MetalPluginClassName'], 'NativeAdrenoMetalDevice')
        self.assertEqual(adreno.START_PERSONALITY['IOClass'], 'NativeGpuStart')

    def test_adreno_stack_only_in_the_hardware_bundle(self):
        import build_macos26_storage as storage
        with self.assertRaisesRegex(ValueError, 'hardware .UFS. bundle'):
            storage.build(ROOT, ROOT, {}, ROOT, None, {}, {}, transport='virtio', gop_display=True, gpu=True)
        with self.assertRaisesRegex(ValueError, 'hardware .UFS. bundle'):
            storage.build(ROOT, ROOT, {}, ROOT, None, {}, {}, transport='ufs', gop_display=False, gpu=True)


class FirmwareTests(unittest.TestCase):
    def test_pins_cover_the_three_files_and_the_header_matches(self):
        pins = adreno.firmware_pins()
        self.assertEqual([pin['property'] for pin in pins], ['sqe-firmware', 'gmu-firmware', 'zap-firmware'])
        header = adreno.pins_header()
        for pin in pins:
            self.assertIn(f'"{pin["property"]}", {pin["bytes"]}u', header)
            self.assertIn('0x' + pin['sha256'][:2] + ',0x' + pin['sha256'][2:4], header)

    def test_local_firmware_must_match_every_pin(self):
        pins = [dict(property=name, source=name + '.bin', bytes=4, sha256=hashlib.sha256(name.encode()[:4].ljust(4, b'x')).hexdigest())
                for name in ('sqe', 'gmu', 'zap')]
        original = adreno.firmware_pins
        adreno.firmware_pins = lambda root=ROOT: pins
        self.addCleanup(setattr, adreno, 'firmware_pins', original)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for name in ('sqe', 'gmu', 'zap'):
                (directory / (name + '.bin')).write_bytes(name.encode()[:4].ljust(4, b'x'))
            properties = adreno.checked_firmware(directory)
            self.assertEqual(properties['sqe'], b'sqex')
            (directory / 'zap.bin').write_bytes(b'nope')
            with self.assertRaisesRegex(ValueError, 'differs from the pinned firmware'):
                adreno.checked_firmware(directory)

    def test_no_firmware_is_fetched_or_embedded(self):
        source = (ROOT / 'scripts/native_adreno.py').read_text()
        self.assertNotIn('sheng-firmware', source)
        self.assertNotIn('.incbin', source)
        self.assertIn("inputs(root)['linux']", source)
        front = (ROOT / adreno.PLATFORM_DIR / adreno.FRONT_END).read_text()
        self.assertIn('/arm-io/native-gpu', front)
        self.assertIn('PE_vmm_present != 0', front)
        self.assertIn('PE_parse_boot_argn("sheng-gpu-power"', front)


class PatchTests(unittest.TestCase):
    def test_cpu_pages_use_kernel_page_numbers_not_a_fixed_shift(self):
        lines = added(CPU_PATCH)
        text = '\n'.join(lines)
        self.assertIn('physical>>PAGE_SHIFT', text)
        self.assertNotRegex(text, r'>>\s*12')
        self.assertIn('pmap_set_cache_attributes(cpuPageNumber(page)', text)
        # 16 KiB granules in TCR_EL1: TG0 = 2, TG1 = 1; the 4 KiB encodings stay for the upstream host tests.
        self.assertIn('((out->tcr>>14)&3)==(small?0u:2u)', text)
        self.assertIn('((out->tcr>>30)&3)==(small?2u:1u)', text)

    def test_page_walk_arithmetic(self):
        # The helper expressions from the patch, in Python, for both page sizes.
        for size in (4096, 16384):
            first = lambda physical: physical & ~(size - 1)
            end = lambda physical, bytes_: (physical + bytes_ + size - 1) & ~(size - 1)
            pages = lambda physical, bytes_: list(range(first(physical), end(physical, bytes_), size))
            self.assertEqual(pages(size, size), [size])
            self.assertEqual(pages(size + 4096, 4096)[0], first(size + 4096))
            self.assertEqual(len(pages(0x80000000, size * 3)), 3)
        self.assertEqual(len(list(range(0x80001000 & ~0x3fff, (0x80001000 + 0x8000 + 0x3fff) & ~0x3fff, 0x4000))), 3)

    def test_window_server_is_the_render_server(self):
        self.assertIn('"WindowServer"', '\n'.join(added(CPU_PATCH)))

    def test_board_patch_keeps_firmware_out_and_drops_weak_kernel_imports(self):
        lines = added(BOARD_PATCH)
        text = '\n'.join(lines)
        self.assertIn('const uint8_t *native_adreno_zap_image;', text)
        self.assertNotRegex(text, r'native_adreno_\w*firmware\w*.*weak')
        for symbol in ('armcpu_spi_unhandled', 'vm_page_free_count', 'armcpu_ipi_interrupts'):
            matching = [line for line in lines if symbol in line and 'extern' in line]
            self.assertTrue(matching, symbol)
            self.assertFalse(any('weak' in line for line in matching), symbol)
        self.assertIn('gMetalStage = 5;', text)
        self.assertNotIn('OSKextLoadKextWithIdentifier', text)

    def test_patch_references_only_upstream_files(self):
        for patch in (CPU_PATCH, BOARD_PATCH):
            names = re.findall(r'^\+\+\+ b/(.+)$', patch.read_text(), re.M)
            self.assertTrue(names)
            self.assertTrue(all(name.endswith(('.cpp', '.h')) for name in names), names)


class BoardGateTests(unittest.TestCase):
    def test_stage_one_never_writes_and_respects_every_gate(self):
        header = ROOT / adreno.BOARD / 'GpuPowerBoard.h'
        compiler = shutil.which('gcc') or shutil.which('clang')
        if not header.exists() or not compiler:
            self.skipTest('needs the upstream checkout and a C compiler')
        with tempfile.TemporaryDirectory() as temporary:
            program = Path(temporary) / 'board'
            build = subprocess.run([compiler, '-std=gnu17', '-O1', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', '-Wno-unused-but-set-variable', '-I', str(header.parent),
                                    str(ROOT / 'tests/native_gpu_board.c'), '-o', str(program)], capture_output=True, text=True, timeout=60)
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run([str(program)], capture_output=True, text=True, timeout=60)
            self.assertEqual((run.returncode, run.stdout.strip()), (0, 'ok'), run.stdout)


class FrontEndTests(unittest.TestCase):
    def test_the_accelerator_is_published_only_by_the_driver_service(self):
        front = (ROOT / adreno.PLATFORM_DIR / adreno.FRONT_END).read_text()
        self.assertNotIn('native_adreno_publish(', front)
        self.assertIn('if (!PE_parse_boot_argn("sheng-gpu-power", &mode, sizeof(mode)) || !mode) {', front)

    def test_rows_are_printed_again_during_the_first_two_minutes(self):
        front = (ROOT / adreno.PLATFORM_DIR / adreno.FRONT_END).read_text()
        self.assertIn('const bool again = seconds < 120 && seconds % 20 == 18;', front)
        self.assertEqual(front.count('(again || memcmp('), 2)


class DeviceTreeTests(unittest.TestCase):
    PROPERTIES = {'compatible': b'sheng,adreno740\0', 'contract': b'sheng-adreno-v1\0', 'sqe-firmware': b'\x01' * 16, 'gmu-firmware': b'\x02' * 20, 'zap-firmware': b'\x03' * 9}

    @staticmethod
    def node(name, children=()):
        entries = [(b'name', name.encode() + b'\0')]
        data = struct.pack('<II', len(entries), len(children))
        for key, value in entries:
            data += struct.pack('<32sI', key, len(value)) + value + bytes(-len(value) % 4)
        return data + b''.join(children)

    def test_firmware_node_is_added_once_and_only_with_firmware(self):
        from native_devtree import configure_native_text_state
        tree = self.node('device-tree', (self.node('chosen'), self.node('arm-io')))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'kernel.adt'
            path.write_bytes(tree)
            configure_native_text_state(path, development_root=True, gpu_node=self.PROPERTIES)
            once = path.read_bytes()
            self.assertEqual(once.count(b'native-gpu\0'), 1)
            self.assertIn(b'\x02' * 20, once)
            configure_native_text_state(path, development_root=True, gpu_node=self.PROPERTIES)
            self.assertEqual(path.read_bytes(), once)
            with self.assertRaisesRegex(ValueError, 'GPU node'):
                configure_native_text_state(path, development_root=True, gpu_node={'compatible': b'x\0'})

    def test_without_firmware_the_tree_is_not_touched_by_this_driver(self):
        from native_devtree import configure_native_text_state
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'kernel.adt'
            path.write_bytes(self.node('device-tree', (self.node('chosen'), self.node('arm-io'))))
            configure_native_text_state(path, development_root=True)
            self.assertNotIn(b'native-gpu', path.read_bytes())


class PackagerTests(unittest.TestCase):
    COMMON = ['--kernel-build', 'k', '--qemu-observation', 'q', '--bootstrap-observation', 'b', '--default-off-observation', 'd', '--firmware-build', 'f', '--backup-dir', 'x',
              '--output', 'o', '--root-deployment', 'a', '--partition-plan', 'b', '--root-build', 'c', '--root-hash', 'd', '--root-volume-uuid', '808eac5e-8275-478d-814c-2229d8eb8d07']

    def run_packager(self, *arguments):
        import package_macos26_ufs as packager
        stderr = io.StringIO()
        with mock.patch.object(sys, 'argv', ['package', *self.COMMON, *arguments]), contextlib.redirect_stderr(stderr), \
                mock.patch.object(packager, 'qualify', side_effect=AssertionError('qualification reached')):
            with self.assertRaises((SystemExit, AssertionError)):
                packager.main()
        return stderr.getvalue()

    def test_firmware_goes_with_the_stages_that_use_it(self):
        self.assertIn('--gpu-firmware', self.run_packager('--gpu-stage', '7'))
        self.assertIn('--gpu-firmware', self.run_packager('--gpu-firmware', 'fw'))
        self.assertIn('--gpu-firmware', self.run_packager('--gpu-stage', '1', '--gpu-firmware', 'fw'))
        self.assertNotIn('--gpu-firmware', self.run_packager('--gpu-stage', '1'))
        self.assertNotIn('--gpu-firmware', self.run_packager('--gpu-stage', '6'))
        self.assertNotIn('--gpu-firmware', self.run_packager('--gpu-stage', '9', '--gpu-firmware', 'fw'))

    def test_only_the_nine_stages_exist(self):
        self.assertIn('invalid choice', self.run_packager('--gpu-stage', '10'))
        self.assertIn('invalid choice', self.run_packager('--gpu-stage', '0'))

    def test_kernel_must_hold_the_audited_stack(self):
        import package_macos26_ufs as packager
        with tempfile.TemporaryDirectory() as temporary:
            build = Path(temporary)
            for driver in ({'build': {}}, {'build': {'native_adreno': {'schema': 'x'}}}):
                (build / 'security-relink.json').write_text(json.dumps(dict(storage_driver=driver)))
                with self.assertRaisesRegex(ValueError, 'audited Adreno'):
                    packager.qualify_gpu(build)


    def test_plugin_goes_with_stage_9_and_its_trust_cache(self):
        self.assertIn('--gpu-plugin-trust-cache', self.run_packager('--gpu-plugin', 'bundle'))
        self.assertIn('--gpu-plugin-trust-cache', self.run_packager('--gpu-plugin-trust-cache', 'trust'))
        self.assertIn('--gpu-stage 9', self.run_packager('--gpu-stage', '8', '--gpu-firmware', 'fw',
                                                         '--gpu-plugin', 'bundle', '--gpu-plugin-trust-cache', 'trust'))


class PluginTests(unittest.TestCase):
    FRONT = (ROOT / adreno.PLATFORM_DIR / adreno.FRONT_END).read_text()

    def test_install_is_default_off_and_needs_stage_9(self):
        self.assertIn('if (mode < 9 || !PE_parse_boot_argn("sheng-gpu-metal", &metal, sizeof(metal)) || metal != 1) {', self.FRONT)
        self.assertLess(self.FRONT.index('refused in a VM'), self.FRONT.index('startPluginInstall(mode);'))

    def test_kernel_helper_patch_only_adds_and_is_built(self):
        name = 'platform/sheng/macos26/patches/0067-gpu-plugin-kernel-mkdir.patch'
        lines = (ROOT / name).read_text().splitlines()
        self.assertFalse([line for line in lines if line.startswith('-') and not line.startswith('---')])
        self.assertIn(name, (ROOT / 'scripts/build_macos26_xnu.py').read_text())

    def bundle(self, directory):
        macos = Path(directory) / 'NativeAdrenoMetal.bundle/Contents/MacOS'
        macos.mkdir(parents=True)
        (macos.parent / 'Info.plist').write_bytes(b'plist' * 100)
        (macos / adreno.PLUGIN_NAME).write_bytes(b'exec' * 1000)
        (macos / adreno.PLUGIN_TURNIP).write_bytes(b'turnip' * 1000)
        return macos.parent.parent

    def test_node_unpacks_to_the_original_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = self.bundle(temporary)
            node = adreno.plugin_node(bundle)
        self.assertEqual(node['compatible'], b'sheng,gpu-plugin\0')
        size = struct.unpack_from('<Q', node['executable'])[0]
        self.assertEqual(size, 4000)
        self.assertEqual(zlib.decompress(node['executable'][8:]), b'exec' * 1000)

    @staticmethod
    def trust(hashes):
        entries = b''.join(item + bytes([2, 0, 0, 0]) for item in sorted(hashes))
        return struct.pack('<II', 1, 8) + struct.pack('<I', 2) + bytes(16) + struct.pack('<I', len(hashes)) + entries

    def test_trust_cache_may_add_only_the_plugin_hashes(self):
        apple = [bytes([1]) * 20, bytes([2]) * 20]
        plugin = [bytes([3]) * 20, bytes([4]) * 20]
        wanted = tuple(item.hex() for item in plugin)
        with tempfile.TemporaryDirectory() as temporary:
            original, extended = Path(temporary) / 'a', Path(temporary) / 'b'
            original.write_bytes(self.trust(apple))
            extended.write_bytes(self.trust(apple + plugin))
            with mock.patch('system_trust_cache.check_bundle'):
                self.assertEqual(adreno.plugin_trust_cache(original, extended, wanted)['entries'], 4)
                extended.write_bytes(self.trust(apple + plugin + [bytes([5]) * 20]))
                with self.assertRaisesRegex(ValueError, 'exactly the plugin'):
                    adreno.plugin_trust_cache(original, extended, wanted)
                extended.write_bytes(self.trust(apple[:1] + plugin))
                with self.assertRaisesRegex(ValueError, 'exactly the plugin'):
                    adreno.plugin_trust_cache(original, extended, wanted)


if __name__ == '__main__':
    unittest.main()
