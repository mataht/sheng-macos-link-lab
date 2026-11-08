"""Tiered qualification: reuse a reference kernel's long QEMU evidence when only display/input changed.

User policy (2026-11-08): the slow same-kernel QEMU runs exist to protect storage and the boot path. When a new CI build
differs from an already qualified reference build only in the GOP display, recovery input or hardware key companions, the
reference's development/ISC userland evidence still describes this kernel's storage, ISC and boot code, and display/input
behaviour is checked on the hardware instead. The new kernel always gets its own quick QEMU gates (root discovery, UFS
default-off, CN placement, 8 cores) through the packager's normal checks.

The scope is decided from the CI build records, never by hand: every recorded field must be identical except build
output identities (hashes/UUIDs of the linked binaries and archives, which change on every link) and the three
display/input component records. Any other difference -- storage sources, ISC, root publication, macoslog, kernel
patches, sources, recovery UI policy, module list -- requires the full same-kernel runs.
"""
import json
from pathlib import Path

SCHEMA = 'sheng-tiered-qualification-v1'
POLICY = 'tiered qualification approved by the user on 2026-11-08'
# Fields that identify the linked outputs of one CI run (they differ between any two links of the same sources).
OUTPUT_IDENTITY = {
    '/executable_kernel_sha256', '/kernel_uuid', '/dsym_uuid', '/source_archive_sha256', '/source_kernel_sha256',
    '/link_map_sha256', '/corecrypto/archive_sha256', '/corecrypto_integrity/kernel_sha256',
    '/storage_driver/binary_sha256', '/ufs_driver/binary_sha256',
}
# Component records whose behaviour is verified on hardware under the tiered policy.
DISPLAY_INPUT = ('/ufs_driver/native_gop_display', '/ufs_driver/native_recovery_input', '/ufs_driver/native_button_keys')
RELINK_SAME = ('modules', 'recovery_components', 'hid_components', 'qemu_only', 'hardware_admitted')


def differences(old, new, path=''):
    if isinstance(old, dict) and isinstance(new, dict):
        found = []
        for key in sorted(set(old) | set(new)):
            if key not in old or key not in new:
                found.append(path + '/' + key)
            else:
                found += differences(old[key], new[key], path + '/' + key)
        return found
    return [] if old == new else [path]


def in_scope(path):
    return path in OUTPUT_IDENTITY or any(path == p or path.startswith(p + '/') for p in DISPLAY_INPUT)


def compare_scope(build, reference) -> dict:
    """Raise unless `build` differs from `reference` only in output identities and display/input components."""
    build, reference = Path(build), Path(reference)
    new_kernel = json.loads((build / 'build.json').read_text())
    old_kernel = json.loads((reference / 'build.json').read_text())
    changed = differences(old_kernel, new_kernel)
    outside = [p for p in changed if not in_scope(p)]
    if outside:
        raise ValueError('build differs from the qualification reference outside display/input: ' + ', '.join(outside[:8]))
    if new_kernel.get('executable_kernel_sha256') == old_kernel.get('executable_kernel_sha256'):
        raise ValueError('reference and candidate are the same kernel; use the same-kernel evidence directly')
    new_relink = json.loads((build / 'security-relink.json').read_text())
    old_relink = json.loads((reference / 'security-relink.json').read_text())
    for key in RELINK_SAME:
        if new_relink.get(key) != old_relink.get(key):
            raise ValueError('assembled candidate differs from the reference in ' + key)
    for key in ('input_vtable_abi', 'button_keys_vtable_abi', 'touch_vtable_abi', 'display_vtable_abi', 'vtable_abi'):
        new_abi = new_relink['storage_driver'].get(key)
        if new_abi is None:
            continue
        if new_abi.get('pac_metadata_matches') is False:
            raise ValueError('candidate vtable audit failed: ' + key)
    components = sorted({p for p in changed if not p in OUTPUT_IDENTITY})
    return dict(schema=SCHEMA, policy=POLICY, reference=str(reference.resolve()),
                reference_kernel_sha256=old_kernel['executable_kernel_sha256'],
                kernel_sha256=new_kernel['executable_kernel_sha256'],
                changed_display_input_fields=components,
                changed_output_identities=sorted(p for p in changed if p in OUTPUT_IDENTITY),
                reused=['development dyld / ISC userland observation'],
                hardware_verified=['display', 'recovery input', 'hardware keys'])


def display_hardware_verification() -> dict:
    """Display package without same-kernel QEMU display evidence: display behaviour is checked on the tablet."""
    return dict(schema='sheng-display-hardware-verification-v1', policy=POLICY, qemu_display_qualified=False,
                verified_on='hardware', boot_argument='native-gop-display=1 sheng-recovery-ui=1',
                language_selection_qualified=False, input_qualified=False, hardware_display_qualified=False,
                gpu_acceleration=False)
