#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Opt-in provenance and initialization checks for one VirtIO/GOP probe bundle."""
from collections import Counter
import hashlib
import json
import plistlib
import struct

from inspect_macos26_security import backed_offset, chains, exports, macho as kc_macho
from native_macho import macho, symbols, native_chained_fixups

EXPECTED_OBJECTS = {
    'NativeVirtioBlock.o': 'ff34755c0846fc3017cc3a47054a645f335c3c31efdc50e161f9c7e2426d7235',
    'NativeUfs.o': 'e3fea376496dc240ddc9e70982a788d267fea37a409d32a9c552c3441950e8ef',
}
REVIEWED_RENAMES = {
    '_sheng_ufs_kmod_info': '6ecdbac945ad6f23d81f95bc98a1037656240027e80a48f515625696b384ffa2',
    '_ufs_kmod_': '00c7930d1d3d52eaa3510ab020a3a2b68670270d8e22e734fabd488e76a59b08',
}


def validate_ci_source(ci_inputs, source_commit, hashes):
    """Accept the original artifact layout or its hash-pinned persistent packaging."""
    original = ci_inputs.get('original_artifact')
    if original is None:
        original = dict(source_commit=ci_inputs.get('source_commit'),
            artifact_id=ci_inputs.get('artifact_id'), sha256=ci_inputs.get('artifact_sha256'))
    if (original.get('source_commit') != source_commit or original.get('artifact_id') != 12956372647
            or original.get('sha256') != 'bdc4acdc0f34c6d3a85a4e53e120a4f1113a78143bb13447a665077b94b5453f'):
        raise ValueError('Apple CI source is not the fixed reviewed upstream artifact')
    for relative, local in (('storage-driver/NativeVirtioBlock.o', 'NativeVirtioBlock.o'),
                            ('ufs-driver/NativeUfs.o', 'NativeUfs.o')):
        if ci_inputs.get('objects', {}).get(relative, {}).get('sha256') != hashes[local]:
            raise ValueError('Apple CI object identity differs from the local reviewed input')
    if 'input_archive' in ci_inputs:
        archive = ci_inputs['input_archive']
        manifest = ci_inputs.get('bundle_manifest', {})
        if (archive != dict(kind='persistent_driver_objects',
                sha256='6db20d3a711308cdbd8ca312ee198d8ebac7792dd423bb071c50413fc5d37e8c', bytes=980924)
                or manifest.get('format') != 1 or manifest.get('kind') != 'persistent_driver_objects'
                or manifest.get('original_artifact') != original):
            raise ValueError('Apple CI persistent input packaging differs from its reviewed manifest')
        for relative, value in ci_inputs['objects'].items():
            if manifest.get('members', {}).get(relative) != value:
                raise ValueError('persistent object manifest differs from actual CI inputs')


def validate_combined(directory, evidence, info, enabled):
    combined = evidence.get('combined_driver')
    if not combined:
        if enabled or evidence.get('native_gop_display'):
            raise ValueError('VirtIO GOP requires explicit combined-driver provenance')
        return
    if (not enabled or combined.get('schema') != 'sheng-virtio-gop-combined-v1'
            or combined.get('qemu_only') is not True or combined.get('hardware_admitted') is not False
            or combined.get('link_exit_code') != 0
            or combined.get('source_project_commit') != 'a200d204230846bb640b715086d8c627405ba2df'
            or combined.get('primary_kmod_symbol') != '_kmod_info'
            or combined.get('retained_kmod_symbol') not in REVIEWED_RENAMES
            or combined.get('ufs_personalities_enabled') is not False
            or combined.get('ufs_write_capable_code_retained') is not True
            or combined.get('native_recovery_input_linked') is not False
            or evidence.get('native_recovery_input')):
        raise ValueError('unqualified combined driver or missing explicit opt-in')
    inputs = directory / 'combined-inputs'
    required = {*EXPECTED_OBJECTS, 'NativeUfs-renamed.o', 'virtio-build.json', 'ufs-build.json', 'virtio-Info.plist',
                'ufs-Info.plist', 'NativeVirtioBlock.macho', 'NativeUfs.macho'}
    if combined.get('apple_ci'):
        required |= {'ci-inputs.json', 'ci-result.json'}
    hashes = combined.get('inputs_sha256', {})
    if set(hashes) != required:
        raise ValueError('combined driver input manifest is incomplete')
    for name, expected in hashes.items():
        if hashlib.sha256((inputs / name).read_bytes()).hexdigest() != expected:
            raise ValueError('combined driver provenance input changed: ' + name)
    if any(hashes[name] != expected for name, expected in EXPECTED_OBJECTS.items()):
        raise ValueError('combined driver objects differ from the fixed reviewed CI inputs')
    if hashes['NativeUfs-renamed.o'] != REVIEWED_RENAMES[combined['retained_kmod_symbol']]:
        raise ValueError('combined driver secondary kmod rename differs from reviewed bytes')
    if combined.get('apple_ci'):
        ci = json.loads((inputs / 'ci-result.json').read_text())
        ci_inputs = json.loads((inputs / 'ci-inputs.json').read_text())
        binding = combined['apple_ci']
        validate_ci_source(ci_inputs, combined['source_project_commit'], hashes)
        if (ci.get('status') != 'link_probe_passed' or ci.get('run_id') != binding.get('run_id')
                or ci.get('commit') != binding.get('commit') or ci.get('provenance') != ci_inputs
                or ci['outputs']['NativeVirtioGop']['sha256'] != evidence['binary_sha256']
                or ci_inputs['rename']['output_sha256'] != hashes['NativeUfs-renamed.o']):
            raise ValueError('Apple CI link result is not bound to the fixed source and output')
    virtio = json.loads((inputs / 'virtio-build.json').read_text())
    ufs = json.loads((inputs / 'ufs-build.json').read_text())
    if (virtio['binary_sha256'] != hashes['NativeVirtioBlock.macho']
            or ufs['binary_sha256'] != hashes['NativeUfs.macho']
            or evidence.get('native_gop_display') != ufs.get('native_gop_display')):
        raise ValueError('combined driver source modules or GOP record differ')
    for key, value in virtio.items():
        if key != 'binary_sha256' and evidence.get(key) != value:
            raise ValueError('combined driver changed the original VirtIO transport contract: ' + key)
    expected = plistlib.loads((inputs / 'virtio-Info.plist').read_bytes())
    original_ufs = plistlib.loads((inputs / 'ufs-Info.plist').read_bytes())
    display = dict(original_ufs['IOKitPersonalities']['NativeGOPDisplay'])
    display['CFBundleIdentifier'] = evidence['bundle']
    expected['IOKitPersonalities']['NativeGOPDisplay'] = display
    for name in ('com.apple.iokit.IOMobileGraphicsFamily', 'com.apple.iokit.IOSurface', 'com.apple.iokit.IOAVFamily'):
        expected['OSBundleLibraries'][name] = original_ufs['OSBundleLibraries'][name]
    if info != expected:
        raise ValueError('combined driver must expose exactly original VirtIO and paired GOP personalities')


def local_initializers(path):
    """Read existing linked ctor chains without executing or resolving external imports."""
    data = path.read_bytes()
    image = macho(data, 'arm64e')
    own = symbols(path)
    names = {}
    for name, address in own.items():
        names.setdefault(address, []).append(name)
    found = []
    for segment in image['segments']:
        for section in segment['sections']:
            if section['name'] != '__mod_init_func':
                continue
            if section['size'] % 8:
                raise ValueError('unaligned module constructor section')
            for site in range(section['va'], section['va'] + section['size'], 8):
                raw, = struct.unpack_from('<Q', data, backed_offset(image['segments'], site, 8, len(data)))
                if raw >> 62 & 1:
                    raise ValueError('external constructor target is not an original local initializer')
                auth = bool(raw >> 63)
                target = raw & (0xffffffff if auth else (1 << 43) - 1)
                if not any(s['initprot'] & 4 and s['va'] <= target < s['va'] + s['filesize'] for s in image['segments']):
                    raise ValueError('module constructor target is not executable')
                if target not in names:
                    raise ValueError('unnamed module constructor target cannot be compared')
                found.append(dict(names=sorted(names[target]), target=target, site=site,
                    authenticated=auth, key=raw >> 49 & 3, address_diversity=bool(raw >> 48 & 1),
                    diversity=raw >> 32 & 0xffff))
    return found


def audit_combined_metaclasses(driver, original, kc, native, slide, native_data):
    """Default MetaClass macros derive from OSMetaClass, without a Dispatch override."""
    from native_storage_module import audit_storage_vtable
    if not isinstance(native_data, bytes):
        raise ValueError('combined metaclass audit requires the actual native kernel bytes')
    native_chained_fixups(native_data)
    native_image = macho(native_data, 'arm64e')
    native_base = next(s['va'] for s in native_image['segments'] if s['name'] == '__TEXT')
    native_table = native['__ZTV11OSMetaClass']
    kernel = kc_macho(original, kc['entries']['com.apple.kernel'][1])
    names = exports(original, kernel)
    # The reference destructor is a real local function omitted from the export
    # trie. Read bounded defined LC_SYMTAB names for reference identification
    # only; this does not admit any private symbol as an import target.
    symtabs = [at for cmd, at, length in kernel['commands'] if cmd == 2 and length == 24]
    if len(symtabs) != 1:
        raise ValueError('metaclass reference requires the original kernel symbol table')
    offset, count, strings, size = struct.unpack_from('<4I', original, symtabs[0] + 8)
    if offset + count * 16 > len(original) or strings + size > len(original):
        raise ValueError('original metaclass symbols exceed the paired collection')
    for index in range(count):
        name, kind, section, _, address = struct.unpack_from('<IBBHQ', original, offset + index * 16)
        if kind & 0xe0 or kind & 0xe != 0xe or not section:
            continue
        end = original.find(b'\0', strings + name, strings + size)
        if name >= size or end < 0:
            raise ValueError('unterminated original metaclass reference symbol')
        symbol = original[strings + name:end].decode()
        if symbol.startswith(('__ZN11OSMetaClass', '__ZNK11OSMetaClass')):
            names.setdefault(address, [])
            if symbol not in names[address]:
                names[address].append(symbol)
    table = next(a for a, ns in names.items() if '__ZTV11OSMetaClass' in ns)
    original_slots = {p['site']: p for p in chains(original, kc)}
    native_reference = []
    replaced_base_slots = {0: '__ZN11OSMetaClassD1Ev', 1: '__ZN11OSMetaClassD0Ev',
                           21: '___cxa_pure_virtual'}
    for index in range(30):
        old = original_slots[table + 16 + index * 8]
        at = backed_offset(native_image['segments'], native_table + 16 + index * 8, 8, len(native_data))
        raw, = struct.unpack_from('<Q', native_data, at)
        if raw >> 62 & 1:
            raise ValueError('native OSMetaClass contains an unresolved bind')
        auth = bool(raw >> 63)
        target = native_base + (raw & (0xffffffff if auth else (1 << 43) - 1))
        expected = {native[name] for name in names.get(old['target'], []) if name in native}
        if index in replaced_base_slots:
            # Derived defaults replace these three base entries. Verify the
            # actual native declaration and PAC here, and the exact derived
            # destructor/alloc targets below. The original base destructor is
            # stripped, and neither compiled base destructor is invoked by
            # the derived table. Do not pretend its unknown identity matched.
            if (target != native.get(replaced_base_slots[index]) or not any(
                    s['initprot'] & 4 and s['va'] <= old['target'] < s['va'] + s['filesize']
                    for s in kernel['segments'])):
                raise ValueError(f'invalid base declaration for overridden metaclass slot {index}')
        elif target not in expected:
            actual_names = [name for name, value in native.items() if value == target]
            raise ValueError(f'native/original OSMetaClass target differs at slot {index}: '
                             f'{names.get(old["target"], [])} / {actual_names}')
        pac = dict(authenticated=auth, key=raw >> 49 & 3,
            diversity=raw >> 32 & 0xffff, address_diversity=bool(raw >> 48 & 1))
        if any(pac[key] != old[key] for key in pac):
            raise ValueError(f'native/original OSMetaClass PAC differs at slot {index}')
        native_reference.append(dict(slot=index, original_names=names.get(old['target'], []),
            original_target=hex(old['target']), native_target=hex(target),
            comparison='derived override replaces base target' if index in replaced_base_slots
                       else 'native target matches original inherited declaration', **pac))
    if table + 16 + 30 * 8 in original_slots:
        raise ValueError('original OSMetaClass exceeds the reviewed 30-slot table')
    result = {}
    for name in ('NativeVirtioBlock', 'NativeUfsBlock', 'NativeUfs', 'NativeGOPDisplay'):
        audit = audit_storage_vtable(driver, original, kc, native, slide,
            family_bundle='com.apple.kernel', base_class='OSMetaClass', own_class=name + '::MetaClass')
        if audit['slots'] != 30 or {m['slot'] for m in audit['methods'] if m['override']} != {0, 1, 21}:
            raise ValueError('default MetaClass must override only destructors and alloc')
        allocation = '__ZNK' + str(len(name)) + name + '9MetaClass5allocEv'
        exact_overrides = {0: '__ZN' + str(len(name)) + name + '9MetaClassD1Ev',
                           1: '__ZN' + str(len(name)) + name + '9MetaClassD0Ev', 21: allocation}
        for index, symbol in exact_overrides.items():
            if symbol not in (audit['methods'][index]['own_names'] or []):
                raise ValueError(f'default MetaClass slot {index} has the wrong destructor/alloc implementation')
            target = int(audit['methods'][index]['target'], 16)
            if not any(s['initprot'] & 4 and driver['base'] + s['va'] <= target <
                       driver['base'] + s['va'] + s['filesize'] for s in driver['image']['segments']):
                raise ValueError('default MetaClass override is not executable')
        for index in range(30):
            if index not in exact_overrides and audit['methods'][index]['target'] != native_reference[index]['native_target']:
                raise ValueError(f'default MetaClass inherited slot {index} must target the actual native base')
        audit['exact_override_symbols'] = exact_overrides
        result[name] = audit
    return dict(base_class='OSMetaClass', source_macro='OSDeclareDefaultStructors',
        native_reference_sha256=hashlib.sha256(native_data).hexdigest(),
        native_original_inherited_targets_match=True, native_original_pac_matches=True,
        replaced_base_slots=sorted(replaced_base_slots), native_reference=native_reference, classes=result)


def audit_combined_initialization(directory, driver):
    inputs = directory / 'combined-inputs'
    originals = local_initializers(inputs / 'NativeVirtioBlock.macho') + local_initializers(inputs / 'NativeUfs.macho')
    actual = local_initializers(directory / 'NativeVirtioBlock.macho')
    def identity(item):
        return (tuple(item['names']), item['authenticated'], item['key'], item['address_diversity'], item['diversity'])
    if Counter(map(identity, originals)) != Counter(map(identity, actual)):
        raise ValueError('combined module constructors differ from original source modules')
    relocs = {r['new_site']: r for r in driver['relocations']}
    for item in actual:
        r = relocs.get(driver['base'] + item['site'])
        if r is None or r['new_target'] != driver['base'] + item['target']:
            raise ValueError('combined constructor absent from relocated pointer chains')
    original_path = inputs / 'NativeVirtioBlock.macho'
    old_data, old_image, old_symbols = original_path.read_bytes(), macho(original_path.read_bytes()), symbols(original_path)
    old_at = backed_offset(old_image['segments'], old_symbols['_kmod_info'], 196, len(old_data))
    new_at = backed_offset(driver['image']['segments'], driver['symbols']['_kmod_info'] - driver['base'], 196, len(driver['data']))
    if old_data[old_at:old_at + 180] != driver['data'][new_at:new_at + 180]:
        raise ValueError('combined primary kmod identity differs from original VirtIO')
    functions = []
    for offset in (180, 188):
        word, = struct.unpack_from('<Q', old_data, old_at + offset)
        if word >> 62 & 1:
            raise ValueError('original kmod start/stop unexpectedly binds externally')
        old_target = word & (0xffffffff if word >> 63 else (1 << 43) - 1)
        names = sorted(name for name, value in old_symbols.items() if value == old_target)
        r = relocs.get(driver['kmod'] + offset)
        if not names or r is None or not any(driver['symbols'].get(name) == r['new_target'] for name in names):
            raise ValueError('combined kmod start/stop differs from original VirtIO')
        for key, value in dict(authenticated=bool(word >> 63), key=word >> 49 & 3,
                              address_diversity=bool(word >> 48 & 1), diversity=word >> 32 & 0xffff).items():
            if r[key] != value:
                raise ValueError('combined kmod start/stop PAC changed')
        functions.append(dict(offset=offset, names=names, target=hex(r['new_target'])))
    secondary = driver['symbols'].get(driver['evidence']['combined_driver']['retained_kmod_symbol'])
    if secondary is None or secondary == driver['kmod']:
        raise ValueError('retained UFS descriptor is missing or aliases primary kmod')
    return dict(constructor_count=len(actual), constructors=actual,
        same_original_constructor_names_and_pac=True, primary_kmod='NativeVirtioBlock',
        primary_start_stop=functions, secondary_kmod=hex(secondary),
        secondary_kmod_prelinked=False, ufs_hardware_path_enabled=False,
        ufs_write_capable_code_retained=True, runtime_qualified=False)
