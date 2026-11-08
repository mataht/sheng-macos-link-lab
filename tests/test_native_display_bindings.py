# SPDX-License-Identifier: GPL-3.0-or-later
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from native_storage_module import validate_private_slot


class PrivateDisplaySlot(unittest.TestCase):
    def setUp(self):
        self.pointer = dict(authenticated=True, key=0, diversity=4202, address_diversity=True)
        self.allowed = [dict(slot=470, original=self.pointer, new_site=0x1000, new_target=0x2000)]
        self.item = dict(self.pointer, new_site=0x1000, new_target=0x2000)

    def test_exact_inherited_slot_is_admitted(self):
        self.assertEqual(validate_private_slot(self.item, self.allowed)['slot'], 470)

    def test_direct_call_other_slot_or_changed_pac_is_rejected(self):
        for field, value in dict(new_site=0x3000, new_target=0x4000, authenticated=False,
                                 key=1, diversity=0, address_diversity=False).items():
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_private_slot(self.item | {field: value}, self.allowed)

    def test_ambiguous_original_slot_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_private_slot(self.item, self.allowed + copy.deepcopy(self.allowed))


class VirtioDisplayProvenance(unittest.TestCase):
    def test_direct_qemu_display_is_distinct_from_the_combined_driver(self):
        from native_combined_driver import validate_combined
        display = dict(build='25F71')
        validate_combined(Path('.'), dict(native_gop_display=display, qemu_only_display=True), {}, False)
        validate_combined(Path('.'), {}, {}, False)
        for evidence, enabled in ((dict(native_gop_display=display), False), ({}, True),
                                  (dict(native_gop_display=display, qemu_only_display=True), True),
                                  (dict(native_gop_display=display, qemu_only_display=True,
                                        combined_driver=dict(schema='sheng-virtio-gop-combined-v1')), True)):
            with self.subTest(evidence=evidence, enabled=enabled), self.assertRaises(ValueError):
                validate_combined(Path('.'), evidence, {}, enabled)
