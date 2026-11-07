import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from qualification_reuse import compare_scope, display_hardware_verification  # noqa: E402

KERNEL = dict(
    executable_kernel_sha256='aa' * 32, kernel_uuid='UUID: A', dsym_uuid='UUID: A', source_archive_sha256='11' * 32,
    source_kernel_sha256='aa' * 32, link_map_sha256='22' * 32, patches={'p/0053.patch': '33' * 32},
    sources={'xnu': '44' * 32}, corecrypto=dict(archive_sha256='55' * 32, version='x'),
    recovery_ui_policy=dict(default_off=True, page_count=11),
    ufs_driver=dict(binary_sha256='66' * 32, upstream_source_sha256='77' * 32,
                    macoslog=dict(source_sha256={'macoslog_worker.h': '88' * 32}),
                    isc_volatile_overlay=dict(disk_writes=False),
                    native_gop_display=dict(patch_sha256='99' * 32, source_sha256='ab' * 32),
                    native_recovery_input=dict(patch_sha256='ac' * 32),
                    native_button_keys=dict(source_sha256={'spmi_keys.h': 'ad' * 32})),
    storage_driver=dict(binary_sha256='ae' * 32))
RELINK = dict(modules=['com.apple.iokit.IOHIDFamily', 'org.blu-sharky.NativeUfs'], recovery_components=['x'],
              hid_components=['com.apple.iokit.IOHIDFamily'], qemu_only=True, hardware_admitted=False,
              storage_driver=dict(input_vtable_abi=dict(pac_metadata_matches=True),
                                  button_keys_vtable_abi=dict(pac_metadata_matches=True)))


class QualificationReuseTest(unittest.TestCase):
    def builds(self, change=None, relink_change=None):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        reference, build = root / 'reference', root / 'build'
        for path in (reference, build):
            path.mkdir()
        (reference / 'build.json').write_text(json.dumps(KERNEL))
        (reference / 'security-relink.json').write_text(json.dumps(RELINK))
        new = copy.deepcopy(KERNEL)
        for key in ('executable_kernel_sha256', 'source_kernel_sha256'):
            new[key] = 'bb' * 32
        new['kernel_uuid'] = new['dsym_uuid'] = 'UUID: B'
        new['corecrypto']['archive_sha256'] = '56' * 32
        new['ufs_driver']['binary_sha256'] = new['storage_driver']['binary_sha256'] = '67' * 32
        new['ufs_driver']['native_gop_display']['patch_sha256'] = 'ba' * 32
        new['ufs_driver']['native_button_keys']['source_sha256']['spmi_keys.h'] = 'bd' * 32
        new['ufs_driver']['native_button_keys']['chord_window_ms'] = 80
        if change:
            change(new)
        relink = copy.deepcopy(RELINK)
        if relink_change:
            relink_change(relink)
        (build / 'build.json').write_text(json.dumps(new))
        (build / 'security-relink.json').write_text(json.dumps(relink))
        return build, reference

    def test_accepts_display_and_input_only_changes(self):
        result = compare_scope(*self.builds())
        self.assertEqual(result['reference_kernel_sha256'], 'aa' * 32)
        self.assertEqual(result['kernel_sha256'], 'bb' * 32)
        self.assertIn('/ufs_driver/native_gop_display/patch_sha256', result['changed_display_input_fields'])
        self.assertIn('/ufs_driver/native_button_keys/chord_window_ms', result['changed_display_input_fields'])
        self.assertIn('/corecrypto/archive_sha256', result['changed_output_identities'])
        self.assertEqual(result['hardware_verified'], ['display', 'recovery input', 'hardware keys'])

    def test_rejects_storage_kernel_and_policy_changes(self):
        cases = [
            lambda k: k['ufs_driver'].__setitem__('upstream_source_sha256', 'cc' * 32),
            lambda k: k['ufs_driver']['macoslog']['source_sha256'].__setitem__('macoslog_worker.h', 'cc' * 32),
            lambda k: k['ufs_driver']['isc_volatile_overlay'].__setitem__('disk_writes', True),
            lambda k: k['patches'].__setitem__('p/0054.patch', 'cc' * 32),
            lambda k: k['sources'].__setitem__('xnu', 'cc' * 32),
            lambda k: k['corecrypto'].__setitem__('version', 'y'),
            lambda k: k['recovery_ui_policy'].__setitem__('page_count', 12),
            lambda k: k.__setitem__('new_kernel_feature', True),
        ]
        for change in cases:
            with self.assertRaisesRegex(ValueError, 'outside display/input'):
                compare_scope(*self.builds(change))

    def test_rejects_module_changes_failed_audit_and_same_kernel(self):
        with self.assertRaisesRegex(ValueError, 'modules'):
            compare_scope(*self.builds(relink_change=lambda r: r['modules'].append('com.apple.extra')))
        with self.assertRaisesRegex(ValueError, 'vtable audit'):
            compare_scope(*self.builds(relink_change=lambda r: r['storage_driver']['button_keys_vtable_abi'].__setitem__(
                'pac_metadata_matches', False)))

        def same(k):
            k['executable_kernel_sha256'] = 'aa' * 32
        with self.assertRaisesRegex(ValueError, 'same kernel'):
            compare_scope(*self.builds(same))

    def test_display_hardware_verification_record(self):
        record = display_hardware_verification()
        self.assertFalse(record['qemu_display_qualified'])
        self.assertEqual(record['verified_on'], 'hardware')
        self.assertEqual(record['boot_argument'], 'native-gop-display=1 sheng-recovery-ui=1')
        self.assertFalse(record['hardware_display_qualified'] or record['input_qualified'])


if __name__ == '__main__':
    unittest.main()
