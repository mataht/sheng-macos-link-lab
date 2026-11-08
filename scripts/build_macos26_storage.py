#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build pinned native transports against the paired macOS storage headers."""
import hashlib
import json
from pathlib import Path
import plistlib
import subprocess
import uuid

from native_macho import macho
from native_log_file import geometry_defines, layout_of, validate_contract
from native_button_keys import EVENT_DRIVER as BUTTON_KEYS_EVENT_DRIVER, PERSONALITY as BUTTON_KEYS_PERSONALITY
from native_button_keys import evidence as button_keys_evidence
import native_touch
import native_cpufreq

BUNDLE = 'org.unixnu.driver.NativeVirtioBlock'
PROTOCOL_CHARACTERISTICS = {
    'Physical Interconnect': 'Virtual Interface',
    'Physical Interconnect Location': 'External',
}


FULL_ROOT_CONFIG = 'config/macos26-full-root.json'
FULL_ROOT_PARTITION = 'macossystem'
FULL_ROOT_OVERLAY_MIB = (256, 8192)


def full_root_contract(root):
    """Validated full-system root identity; written by prepare_macos26_full_root.py after the image exists."""
    path = root / FULL_ROOT_CONFIG
    if not path.is_file():
        raise ValueError(f'{FULL_ROOT_CONFIG} is missing; derive it from the delivered image first')
    contract = json.loads(path.read_text())
    digest = bytes.fromhex(contract.get('sha256', ''))
    overlay = contract.get('overlay', {})
    if (contract.get('build') != '25F71' or contract.get('variant') != 'full-system'
            or contract.get('window') != 'OS' or len(digest) != 32
            or type(contract.get('bytes')) is not int or contract['bytes'] <= 0 or contract['bytes'] % 4096
            or uuid.UUID(contract.get('volume_uuid', '0' * 32)).int == 0
            or uuid.UUID(contract.get('volume_group', '0' * 32)).int == 0
            or contract.get('partition') != FULL_ROOT_PARTITION
            or overlay != dict(boot_argument='native-ufs-root-overlay-mib', default_off=True,
                               minimum_mib=FULL_ROOT_OVERLAY_MIB[0], maximum_mib=FULL_ROOT_OVERLAY_MIB[1],
                               persistent=False, disk_writes=False)):
        raise ValueError('full-system root contract is incomplete or inconsistent')
    return contract


def build(root, out, compile_record, source, run, record, env, *, transport='virtio', gop_display=False,
          recovery_input=False, button_keys=False, full_root=False, touch=False, cpufreq=False):
    if transport not in ('virtio', 'ufs'):
        raise ValueError('unsupported native storage transport')
    if cpufreq and transport != 'ufs':
        raise ValueError('the CPU frequency companion belongs to the hardware (UFS) storage driver')
    if touch and not gop_display:
        raise ValueError('the touch companion requires the GOP companion')
    if full_root and transport != 'ufs':
        raise ValueError('the full-system root identity belongs to the UFS transport')
    if (button_keys or touch) and transport != 'ufs':
        raise ValueError('the hardware input companions belong to the UFS bundle')
    # The VirtIO bundle may carry the same GOP display and QEMU input companions (QEMU only: the full system's
    # image is a virtio disk there); the hardware never links the VirtIO bundle.
    if recovery_input and not gop_display:
        raise ValueError('the QEMU input experiment requires the GOP companion')
    if button_keys and not gop_display:
        raise ValueError('the hardware key companion requires the GOP companion')
    component = 'NativeVirtioBlock' if transport == 'virtio' else 'NativeUfs'
    bundle = BUNDLE if transport == 'virtio' else 'org.blu-sharky.NativeUfs'
    directory = 'storage-driver' if transport == 'virtio' else 'ufs-driver'
    destination = out / directory
    destination.mkdir(exist_ok=True)
    display = None
    input_driver = None
    if gop_display:
        from prepare_macos26_gop import prepare
        display = prepare(root, destination / 'gop-display')
    if recovery_input:
        from prepare_macos26_input import prepare
        input_driver = prepare(root, destination / 'recovery-input')
    upstream = root / 'UniXNU/drivers' / component / (component + '.cpp')
    include = destination / 'include/IOKit/storage'
    include.mkdir(parents=True, exist_ok=True)
    for header in (root / 'sources/macos26/IOStorageFamily').glob('*.h'):
        (include / header.name).write_bytes(header.read_bytes())
    wrapper = destination / (component + '.cpp')
    entry_prefix = 'sheng_virtio' if transport == 'virtio' else 'sheng_ufs'
    recovery_prefix = ''
    recovery_root = None
    if transport == 'ufs':
        # The full-system root reuses the compiled window-identity mechanism (OS window bytes, digest
        # and the volume UUID to wait for) with the delivered install's values, plus its RAM overlay.
        recovery_root = (full_root_contract(root) if full_root else
                         json.loads((root / 'config/macos26-recovery-root.json').read_text()))
        digest = bytes.fromhex(recovery_root['sha256'])
        identity = str(uuid.UUID(recovery_root['volume_uuid'])).upper()
        if not full_root and (recovery_root['build'] != '25F71' or recovery_root['variant'] != 'recoveryos'
                or recovery_root['window'] != 'OS' or len(digest) != 32
                or recovery_root['bytes'] <= 0 or recovery_root['bytes'] % 512):
            raise ValueError('requires the paired read-only recovery root contract')
        recovery_prefix = ('#include <libkern/libkern.h>\n'
            f'#define NATIVE_UFS_RECOVERY_BYTES {recovery_root["bytes"]}ULL\n'
            '#define NATIVE_UFS_RECOVERY_DIGEST {' + ','.join(str(b) for b in digest) + '}\n'
            f'#define NATIVE_UFS_RECOVERY_UUID "{identity}"\n')
        if full_root:
            recovery_prefix += ('#define NATIVE_UFS_FULL_ROOT 1\n'
                f'#define NATIVE_UFS_FULL_ROOT_OVERLAY "{root / "platform/sheng/macos26/storage/full_root_overlay.h"}"\n')
        if 'isc' in recovery_root:
            isc = recovery_root['isc']
            isc_digest = bytes.fromhex(isc['sha256'])
            isc_uuid = str(uuid.UUID(isc['volume_uuid'])).upper()
            if (isc['window'] != 'ISC' or len(isc_digest) != 32 or not 0 < isc['bytes'] <= 4 << 30
                    or isc['bytes'] % 4096 or isc['volatile_overlay_limit_bytes'] != 64 << 20
                    or isc['sep_trusted_storage'] is not False):
                raise ValueError('requires the bounded owned ISC seed contract')
            recovery_prefix += (f'#define NATIVE_UFS_ISC_BYTES {isc["bytes"]}ULL\n'
                '#define NATIVE_UFS_ISC_DIGEST {' + ','.join(str(b) for b in isc_digest) + '}\n'
                f'#define NATIVE_UFS_ISC_UUID "{isc_uuid}"\n')
        recovery_prefix += f'#include "{root / "platform/sheng/macos26/storage/recovery_root.h"}"\n'
        log = json.loads((root / 'config/macoslog.json').read_text())
        validate_contract(log)
        recovery_prefix += '#define NATIVE_MACOSLOG 1\n'
        # One reviewed layout per contract; the write guard, worker and checks share this exact macro set.
        for name, value in geometry_defines(layout_of(log)).items():
            recovery_prefix += f'#define {name} {value}\n'
        for name, value in (('FILE', uuid.UUID(log['file_uuid']).bytes),
                            ('PARTITION', uuid.UUID(log['partition_uuid']).bytes_le),
                            ('TYPE', uuid.UUID(log['partition_type']).bytes_le)):
            recovery_prefix += '#define NATIVE_MACOSLOG_' + name + '_ID {' + ','.join(map(str, value)) + '}\n'
        recovery_prefix += f'#define NATIVE_MACOSLOG_WORKER "{root / "platform/sheng/macos26/storage/macoslog_worker.h"}"\n'
    wrapper.write_text(
        '#include <TargetConditionals.h>\n'
        'static_assert(TARGET_OS_OSX == 1, "requires macOS storage ABI");\n'
        + recovery_prefix +
        f'#include "{upstream}"\n' +
        (f'#include "{display["source"]}"\n' if display else '') +
        '#include <mach/kmod.h>\n'
        f'extern "C" kern_return_t {entry_prefix}_start(kmod_info_t *, void *) {{ return KERN_SUCCESS; }}\n'
        f'extern "C" kern_return_t {entry_prefix}_stop(kmod_info_t *, void *) {{ return KERN_FAILURE; }}\n'
        f'KMOD_EXPLICIT_DECL({bundle}, "1.0.0", {entry_prefix}_start, {entry_prefix}_stop)\n')
    obj = destination / (component + '.o')
    arguments = compile_record['arguments']
    command, index = [], 0
    while index < len(arguments):
        arg = arguments[index]
        if arg == compile_record['file']:
            command.append(str(wrapper))
        elif arg in ('-o', '-MF', '-MT', '-MQ', '-MJ'):
            if arg == '-o':
                command += ['-o', str(obj)]
            index += 1
        elif arg == '-Xclang' and arguments[index + 1].startswith('-W'):
            index += 1
        elif arg.startswith('-W'):
            pass
        elif arg not in ('-MMD', '-MD', '-MP'):
            command.append(arg)
        index += 1
    command[1:1] = ['-I', str(destination / 'include')]
    if transport == 'virtio':
        command[1:1] = ['-DNATIVE_QEMU_SCRATCH_BLOCK=1']
    else:
        command[1:1] = ['-DNATIVE_UFS_ROOT_PARTITION_NAME="' + (FULL_ROOT_PARTITION if full_root else 'macosroot') + '"']
    sdk = Path(subprocess.check_output(['xcrun', '--sdk', 'macosx', '--show-sdk-path'], text=True).strip())
    command += ['-I', str(source / 'osfmk'), '-I', str(sdk / 'usr/include'),
                '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
                '-Wno-c99-designator', '-Wno-reorder-init-list',
                '-Wno-deprecated-enum-enum-conversion', '-Wno-unnecessary-virtual-specifier']
    run(command, directory + '-compile', record, cwd=Path(compile_record['directory']), env=env)
    objects = [str(obj)]
    if input_driver:
        input_object = destination / 'recovery-input/NativeRecoveryInput.o'
        input_command = [str(input_object) if arg == str(obj) else
                         input_driver['source'] if arg == str(wrapper) else arg for arg in command]
        # Kernel HID's parser conditionally includes MacTypes whenever the user SDK is visible.
        user_headers = input_command.index(str(sdk / 'usr/include'))
        if input_command[user_headers - 1] != '-I':
            raise ValueError('unexpected user SDK include option')
        del input_command[user_headers - 1:user_headers + 1]
        input_command[1:1] = ['-I', str(destination / 'recovery-input/include')]
        input_command += ['-I', str(sdk / 'System/Library/Frameworks/Kernel.framework/Versions/A/Headers')]
        run(input_command, directory + '-input-compile', record,
            cwd=Path(compile_record['directory']), env=env)
        objects.append(str(input_object))
    if button_keys:
        from prepare_macos26_input import prepare_apple_hid_headers
        keys = destination / 'button-keys'
        keys.mkdir(exist_ok=True)
        prepare_apple_hid_headers(root, keys)
        for name in ('native_button_keys.cpp', 'spmi_keys.h'):
            (keys / name).write_bytes((root / 'platform/sheng/macos26/input' / name).read_bytes())
        keys_object = keys / 'NativeButtonKeys.o'
        keys_command = [str(keys_object) if arg == str(obj) else
                        str(keys / 'native_button_keys.cpp') if arg == str(wrapper) else arg for arg in command]
        # Same header arrangement as the input companion: kernel HID headers without the user SDK.
        user_headers = keys_command.index(str(sdk / 'usr/include'))
        if keys_command[user_headers - 1] != '-I':
            raise ValueError('unexpected user SDK include option')
        del keys_command[user_headers - 1:user_headers + 1]
        keys_command[1:1] = ['-I', str(keys / 'include')]
        keys_command += ['-I', str(sdk / 'System/Library/Frameworks/Kernel.framework/Versions/A/Headers')]
        run(keys_command, directory + '-button-keys-compile', record, cwd=Path(compile_record['directory']), env=env)
        objects.append(str(keys_object))
    if touch:
        from prepare_macos26_input import prepare_apple_hid_headers
        touch_dir = destination / 'touch'
        touch_dir.mkdir(exist_ok=True)
        prepare_apple_hid_headers(root, touch_dir)
        for name in native_touch.PLATFORM_SOURCES:
            (touch_dir / name).write_bytes((root / 'platform/sheng/macos26/input' / name).read_bytes())
        include_dirs = sorted({str((root / name).parent) for name in (native_touch.UPSTREAM_CPP, *native_touch.UPSTREAM_HEADERS)})
        base = list(command)
        user_headers = base.index(str(sdk / 'usr/include'))
        if base[user_headers - 1] != '-I':
            raise ValueError('unexpected user SDK include option')
        del base[user_headers - 1:user_headers + 1]
        base[1:1] = ['-I', str(touch_dir / 'include'), '-I', str(touch_dir)]
        base += ['-I', str(sdk / 'System/Library/Frameworks/Kernel.framework/Versions/A/Headers')]
        for include in include_dirs:
            base += ['-I', include]

        def compile_one(source_file, object_file, language):
            one = [str(object_file) if arg == str(obj) else str(source_file) if arg == str(wrapper) else arg for arg in base]
            if language == 'c':
                one = ['-xc' if arg == '-xc++' else '-std=gnu17' if arg.startswith('-std=gnu++') else arg for arg in one
                       if arg not in ('--driver-mode=g++', '-fsized-deallocation', '-fapple-kext',
                                      '-fc++-static-destructors=none')]
                one += ['-Wno-unused-command-line-argument']
            run(one, directory + '-touch-' + object_file.stem + '-compile', record,
                cwd=Path(compile_record['directory']), env=env)
            objects.append(str(object_file))

        compile_one(touch_dir / 'native_touch.cpp', touch_dir / 'NativeTouch.o', 'c++')
        compile_one(root / native_touch.UPSTREAM_CPP, touch_dir / 'TouchProbe.o', 'c++')
        for source_file in native_touch.UPSTREAM_C:
            compile_one(root / source_file, touch_dir / (Path(source_file).stem + '.o'), 'c')
    if cpufreq:
        cpu_dir = destination / 'cpufreq'
        cpu_dir.mkdir(exist_ok=True)
        for name in native_cpufreq.PLATFORM_SOURCES:
            (cpu_dir / name).write_bytes((root / native_cpufreq.PLATFORM_DIR / name).read_bytes())
        cpu_base = list(command)
        user_headers = cpu_base.index(str(sdk / 'usr/include'))
        if cpu_base[user_headers - 1] != '-I':
            raise ValueError('unexpected user SDK include option')
        del cpu_base[user_headers - 1:user_headers + 1]
        cpu_base[1:1] = ['-I', str(cpu_dir)]
        cpu_base += ['-I', str(sdk / 'System/Library/Frameworks/Kernel.framework/Versions/A/Headers')]
        for source_file, object_file in ((cpu_dir / 'native_cpufreq.cpp', cpu_dir / 'NativeCpuFreq.o'),
                                         (root / native_cpufreq.UPSTREAM_CPP, cpu_dir / 'CpuFreqProbe.o')):
            one = [str(object_file) if arg == str(obj) else str(source_file) if arg == str(wrapper) else arg
                   for arg in cpu_base]
            run(one, directory + '-cpufreq-' + object_file.stem + '-compile', record,
                cwd=Path(compile_record['directory']), env=env)
            objects.append(str(object_file))
    binary = destination / (component + '.macho')
    run(['xcrun', 'ld', '-arch', 'arm64e', '-kext', '-kexts_use_stubs',
         '-platform_version', 'macos', '26.5', '26.5',
         '-undefined', 'dynamic_lookup', '-fixup_chains',
         '-no_adhoc_codesign', '-headerpad', '0x1000',
         *objects, '-o', str(binary)], directory + '-link', record, env=env)
    image = macho(binary.read_bytes(), 'arm64e')
    if (image['kind'] != 11 or not image['subtype'] & 0x40000000
            or not any(c == 0x80000034 for c, _, _ in image['commands'])):
        raise ValueError('standalone transport requires an ARM64E bundle with chained imports')
    info = dict(CFBundleIdentifier=bundle, CFBundleVersion='1.0.0',
                CFBundlePackageType='KEXT', CFBundleExecutable=component,
                OSBundleRequired='Root', OSBundleLibraries={
                    'com.apple.iokit.IOStorageFamily': '2.1', 'com.apple.kpi.iokit': '25.0',
                    'com.apple.kpi.libkern': '25.0', 'com.apple.kpi.mach': '25.0',
                    'com.apple.kpi.private': '25.0'},
                IOKitPersonalities={'NativeVirtioBlock': dict(CFBundleIdentifier=BUNDLE,
                    IOClass='NativeVirtioBlock', IOProviderClass='IOPlatformDevice',
                    IONameMatch='virtio,mmio', IOProbeScore=1000,
                    **{'Protocol Characteristics': dict(PROTOCOL_CHARACTERISTICS)})})
    if transport == 'ufs':
        info['IOKitPersonalities'] = {
            'NativeUfs': dict(CFBundleIdentifier=bundle, IOClass=component,
                              IOProviderClass='IOPlatformDevice', IOProbeScore=1000),
            'NativeUfsRoot': dict(CFBundleIdentifier=bundle, IOClass=component,
                                  IOProviderClass='IOPlatformExpertDevice',
                                  IOMatchCategory='NativeUfsRoot', IOProbeScore=500),
        }
    if display:
        info['IOKitPersonalities']['NativeGOPDisplay'] = dict(
            CFBundleIdentifier=bundle, IOClass='NativeGOPDisplay',
            IOProviderClass='IOPlatformExpertDevice', IOMatchCategory='NativeGOPDisplay',
            IOProbeScore=500)
        info['OSBundleLibraries'].update({
            'com.apple.iokit.IOMobileGraphicsFamily': '1.0.0',
            'com.apple.iokit.IOSurface': '1.0.0', 'com.apple.iokit.IOAVFamily': '1.0.0'})
    if input_driver:
        info['IOKitPersonalities']['NativeRecoveryInput'] = dict(
            CFBundleIdentifier=bundle, IOClass='NativeRecoveryInput',
            IOProviderClass='IOPlatformDevice', IONameMatch='native-recovery-input',
            IOMatchCategory='NativeRecoveryInput', IOProbeScore=500)
        info['IOKitPersonalities']['NativeRecoveryHIDEventDriver'] = dict(
            CFBundleIdentifier='com.apple.iokit.IOHIDFamily', IOClass='IOHIDEventDriver',
            IOProviderClass='IOHIDInterface', VendorID=6900, ProductID=4178, IOProbeScore=3000)
        info['OSBundleLibraries']['com.apple.iokit.IOHIDFamily'] = '2.0.0'
    if button_keys:
        info['IOKitPersonalities']['NativeButtonKeys'] = dict(BUTTON_KEYS_PERSONALITY, CFBundleIdentifier=bundle)
        info['IOKitPersonalities']['NativeButtonHIDEventDriver'] = dict(BUTTON_KEYS_EVENT_DRIVER)
        info['OSBundleLibraries']['com.apple.iokit.IOHIDFamily'] = '2.0.0'
    if touch:
        info['IOKitPersonalities']['NativeTouch'] = dict(native_touch.PERSONALITY, CFBundleIdentifier=bundle)
        info['IOKitPersonalities']['NativeTouchHIDEventDriver'] = dict(native_touch.EVENT_DRIVER)
        info['OSBundleLibraries']['com.apple.iokit.IOHIDFamily'] = '2.0.0'
    if cpufreq:
        info['IOKitPersonalities']['NativeCpuFreq'] = dict(native_cpufreq.PERSONALITY, CFBundleIdentifier=bundle)
    (destination / 'Info.plist').write_bytes(plistlib.dumps(info))
    evidence = dict(bundle=bundle, upstream_source_sha256=hashlib.sha256(upstream.read_bytes()).hexdigest(),
                    binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
                    architecture='arm64e', deployment='macOS 26.5', read_only_transport=transport == 'ufs',
                    explicit_qemu_scratch_writes=transport == 'virtio',
                    hardware_enable_required=transport == 'ufs', root_transport_read_only=True,
                    original_storage_runtime_qualified=False, hardware_admitted=False)
    if display:
        evidence['native_gop_display'] = display
        if transport == 'virtio':
            evidence['qemu_only_display'] = True
    if input_driver:
        evidence['native_recovery_input'] = input_driver
    if button_keys:
        evidence['native_button_keys'] = button_keys_evidence(root)
    if touch:
        evidence['native_touch'] = native_touch.evidence(root)
    if cpufreq:
        evidence['native_cpufreq'] = native_cpufreq.evidence(root)
    if transport == 'ufs':
        evidence['macoslog'] = dict(contract=log, layout=layout_of(log)['name'],
            layout_defines=geometry_defines(layout_of(log)), default_off=True,
            boot_argument='native-ufs-macoslog', read_only_mode=1, write_mode=2,
            writes_confined_to_preallocated_file=True, filesystem_metadata_writes=False,
            source_sha256={name: hashlib.sha256((root / 'platform/sheng/macos26/storage' / name).read_bytes()).hexdigest()
                           for name in ('macoslog_layout.h', 'macoslog_worker.h')})
        if full_root:
            evidence['full_root_overlay'] = dict(recovery_root['overlay'], volume_group=recovery_root['volume_group'],
                header_sha256=hashlib.sha256((root / 'platform/sheng/macos26/storage/full_root_overlay.h').read_bytes()).hexdigest(),
                seed_read_only=True, hardware_qualified=False)
            evidence['full_root_writes'] = dict(boot_argument='native-ufs-root-writes=1', default_off=True,
                window_only=True, header_excluded=True, fail_stop=True, exclusive_with_overlay=True,
                force_unit_access_honoured=True, cache_flush='SYNCHRONIZE CACHE(10)', hardware_qualified=False,
                header_sha256=hashlib.sha256((root / 'platform/sheng/macos26/storage/full_root_writes.h').read_bytes()).hexdigest())
            evidence['full_system_root'] = True  # absent from Recovery builds, keeping their records unchanged
        evidence.update(root_partition=FULL_ROOT_PARTITION if full_root else 'macosroot', root_lun=0,
                        android_userdata_used=False, root_media_layout_qualified=False,
                        recovery_root=recovery_root,
                        recovery_disk_image_protocol=dict(PROTOCOL_CHARACTERISTICS),
                        recovery_root_header_sha256=hashlib.sha256((root / 'platform/sheng/macos26/storage/recovery_root.h').read_bytes()).hexdigest(),
                        root_publication_sha256=hashlib.sha256((upstream.parent / 'RootPublication.h').read_bytes()).hexdigest(),
                        recovery_transport_is_not_authentication=True)
        if 'isc' in recovery_root:
            evidence['isc_volatile_overlay'] = dict(seed_read_only=True, disk_writes=False,
                limit_bytes=64 << 20, boot_argument='native-ufs-isc-overlay=1', persistent=False,
                production_header_sha256=hashlib.sha256((upstream.parent / 'RootWindowOverlay.h').read_bytes()).hexdigest(),
                sparse_disk_header_sha256=hashlib.sha256((upstream.parent.parent / 'NativeRamBlock/SparseDisk.h').read_bytes()).hexdigest())
    (destination / 'build.json').write_text(json.dumps(evidence, indent=2) + '\n')
    record['storage_driver' if transport == 'virtio' else 'ufs_driver'] = evidence
