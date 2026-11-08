# SPDX-License-Identifier: GPL-3.0-or-later
"""Adreno 740 kernel stack: default-off contract, firmware pins, the 16 KiB patch and the board patch (no Apple or vendor content)."""
import hashlib
from pathlib import Path
import re
import sys
import tempfile
import unittest

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


if __name__ == '__main__':
    unittest.main()
