#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Package a qualified native UFS root experiment for temporary sheng RAM boot."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import tomllib
import uuid

from observe_macos26_kernel import identify, ROOT, NATIVE_BOOT_ARGS
from package_reference_xnu import vendor_dtbs
from native_devtree import configure_native_text_state
from build_macos26_storage import PROTOCOL_CHARACTERISTICS
from native_log_file import CONTRACT as LOG_CONTRACT, LAYOUT as LOG_LAYOUT, BLOCK as LOG_BLOCK, decode as decode_log_file
from recovery_ui_evidence import qualify_language_selector
from display_diagnostic_evidence import qualify_display_observation
from display_waiver_evidence import qualify_display_waiver
from capture_persistent_log import RAM_RANGES as LOG_RAM_RANGES

# 12 GiB of the 13 GiB contiguous run 0x8C0000000..0xC00000000 (uefi-sheng 0004 on the CN 16 GB unit); the
# last GiB is headroom for UEFI's own top-down allocations. DarwinBoot/ADT accept up to 13 GiB (0041/0009).
OWNED_MEMORY = 0x300000000
OWNED_MEMORY_FIRMWARE = ('uefi-sheng/0004-sheng-cn-16gb-memory-map', 'UniXNU/0041-darwinboot-13gib-window',
                         'UniXNU/0042-native-log-saver-cn-layout', 'UniXNU-sheng/0009-adt-13gib-owned-memory')


def stock_version_for_v0(boot: bytes, image_bytes: int, target_header_version: int) -> int:
    """Preserve the stock v4 field, without qualifying the tablet's v0 loader."""
    if target_header_version != 0:
        raise ValueError('requires the configured v0 transport')
    if len(boot) != 4096 or image_bytes < len(boot) or boot[:8] != b'ANDROID!':
        raise ValueError('requires a complete backed-up slot A v4 header')
    kernel_size, ramdisk_size, os_version, header_size = struct.unpack_from('<4I', boot, 8)
    if (struct.unpack_from('<I', boot, 40)[0] != 4 or header_size != 1584
            or boot[24:40] != bytes(16) or not kernel_size):
        raise ValueError('invalid backed-up slot A v4 header geometry')
    signature_size = struct.unpack_from('<I', boot, 1580)[0]
    used = 4096 + sum((size + 4095) // 4096 * 4096
                      for size in (kernel_size, ramdisk_size, signature_size))
    if used > image_bytes:
        raise ValueError('stock v4 payload exceeds the backed-up image')
    # Android 13+ GKI v4 uses zero here and obtains version information from AVB.
    # https://source.android.com/docs/core/architecture/bootloader/boot-image-header
    # Copy all original bits; neither zero nor nonzero proves v0 ABL acceptance.
    return os_version


def qualify(build, observation, bootstrap, default_off, installed, plan, root_build, *, with_isc=False):
    kernel, symbols, identity = identify(build)
    kernel_build = json.loads((build / 'build.json').read_text())
    for name in ('0051-native-multiline-panic-panel.patch', '0052-native-independent-panic-surface.patch'):
        panel_patch = ROOT / 'platform/sheng/macos26/patches' / name
        if kernel_build.get('patches', {}).get(str(panel_patch.relative_to(ROOT))) != hashlib.sha256(panel_patch.read_bytes()).hexdigest():
            raise ValueError('requires the compiled independent multiline panic panel')
    observed = json.loads((observation / 'result.json').read_text())
    geometry = observed.get('observed_globals', {})
    normal_rows = int(geometry.get('_sheng_console_rows', '0x0'), 16)
    traced = json.loads((bootstrap / 'result.json').read_text())
    refused = json.loads((default_off / 'gdb-stop.json').read_text())
    relink = json.loads((build / 'security-relink.json').read_text())
    media = json.loads(installed.read_text())
    partition = json.loads(plan.read_text())
    root = json.loads(root_build.read_text())
    if (observed.get('kernel_sha256') != identity['kernel_sha256']
            or observed.get('uuid') != identity['uuid'] or observed.get('reached') is not True
            or observed.get('checkpoint') != 'IOFindBSDRoot'
            or observed.get('source_crypto_initialized') is not True
            or observed.get('bootstrap_diagnostic_disarmed') is not True
            or observed.get('observed_globals', {}).get('_sheng_panel_ready') != '0x1'
            or not 6 <= normal_rows <= 24
            or geometry.get('_sheng_panel_bound_rows') != geometry.get('_sheng_console_rows')
            or observed.get('observed_globals', {}).get('_sheng_panic_panel_bound_rows') != '0x10'
            or observed.get('observed_globals', {}).get('_sheng_panel_witness') != '0x1'):
        raise ValueError('requires this exact native kernel at the qualified root-discovery checkpoint')
    for name in ('_g_crypto_funcs', '_img4if', '_amfi', '_libCoreEntitlements', '_pthread_functions'):
        interface = observed.get('original_component_interfaces', {}).get(name, {})
        if interface.get('registered') is not True or interface.get('within_original_module') is not True:
            raise ValueError('missing original startup interface: ' + name)
    if (traced.get('kernel_sha256') != identity['kernel_sha256'] or traced.get('uuid') != identity['uuid']
            or traced.get('reached_c_entry') is not True or traced.get('instruction_and_args_match') is not True
            or traced.get('bootstrap_stage') != '0xf'
            or traced.get('post_mmu_framebuffer_mapping_ready') is not True
            or traced.get('actual_stage_pixels') != ['0xff00ff00'] * 16
            # The trace must reproduce this candidate's physical placement for the packaged 12 GiB window.
            or traced.get('placement') != 'cn-16gb-12gib' or traced.get('owned_window_bytes') != hex(OWNED_MEMORY)):
        raise ValueError('requires matching sheng physical-placement and framebuffer bootstrap execution')
    driver = relink['storage_driver']
    recovery = json.loads((ROOT / 'config/macos26-recovery-root.json').read_text())
    if (relink.get('candidate_sha256') != identity['kernel_sha256']
            or driver['bundle'] != 'org.blu-sharky.NativeUfs'
            or driver['build'].get('root_partition') != 'macosroot'
            or driver['build'].get('root_lun') != 0
            or driver['build'].get('android_userdata_used') is not False
            or driver['build'].get('recovery_root') != recovery
            or driver['build'].get('recovery_disk_image_protocol') != PROTOCOL_CHARACTERISTICS
            or driver['build'].get('recovery_transport_is_not_authentication') is not True
            or driver['vtable_abi'].get('block_class') != 'NativeUfsBlock'
            or driver['vtable_abi'].get('pac_metadata_matches') is not True
            or not 1 <= driver['vtable_abi'].get('slots', 0) <= 512
            or refused.get('kernel_sha256') != identity['kernel_sha256']
            or refused.get('function_return_stack_matched') is not True or refused.get('x0') != '0x0'):
        raise ValueError('requires the paired recovery UFS ABI and actual default-off function return')
    for name in ('_native_ufs_probe_requested', '_native_ufs_mapped', '_native_ufs_root_step', '_native_ufs_blocks_published'):
        address = driver['symbols'][name]
        if refused['memory'].get(f'{address:x}:4') != '00000000':
            raise ValueError('UFS default-off runtime touched hardware or published media')
    if (media.get('status') != 'verified' or media.get('readback_verified') is not True
            or media.get('destination') != '/dev/block/by-name/macosroot'
            or media.get('destination_guid') != partition.get('root_unique_guid')
            or media.get('sha256') != media.get('readback_sha256')
            or partition.get('applied') is not True or partition.get('root_image_readback_verified') is not True
            or partition.get('root_image_sha256') != media['sha256']
            or media.get('bytes') != root.get('image_bytes') or root.get('build') != '25F71'
            or media.get('bytes', 0) > partition.get('root_bytes', 0)
            or len(root.get('windows', [])) != (2 if with_isc else 1)
            or any(root['windows'][0].get(k) != recovery[k] for k in ('bytes', 'sha256'))
            or root['windows'][0].get('name') != recovery['window']):
        raise ValueError('requires the written dedicated root extent with matching full readback')
    if with_isc:
        isc = root['windows'][1]
        overlay = driver['build'].get('isc_volatile_overlay', {})
        if (root.get('isc_volatile_overlay') is not True
                or isc.get('name') != 'ISC'
                or any(isc.get(k) != recovery['isc'][k] for k in ('bytes', 'sha256'))
                or isc.get('offset', 0) < root['windows'][0]['offset'] + root['windows'][0]['bytes']
                or isc['offset'] + isc['bytes'] > root['image_bytes']
                or overlay.get('seed_read_only') is not True or overlay.get('disk_writes') is not False
                or overlay.get('persistent') is not False or overlay.get('limit_bytes') != 64 << 20
                or overlay.get('production_header_sha256') != hashlib.sha256(
                    (ROOT / 'UniXNU/drivers/NativeUfs/RootWindowOverlay.h').read_bytes()).hexdigest()
                or overlay.get('sparse_disk_header_sha256') != hashlib.sha256(
                    (ROOT / 'UniXNU/drivers/NativeRamBlock/SparseDisk.h').read_bytes()).hexdigest()):
            raise ValueError('requires the paired isolated ISC seed and bounded volatile overlay')
    return kernel, symbols, identity, media, partition


def qualify_development_dyld(build, observation, identity, root_uuid):
    patch = ROOT / 'platform/sheng/macos26/patches/0053-development-dyld-commpage.patch'
    compiled = json.loads((build / 'build.json').read_text())
    if compiled.get('patches', {}).get(str(patch.relative_to(ROOT))) != hashlib.sha256(patch.read_bytes()).hexdigest():
        raise ValueError('requires the compiled explicit development dyld path')
    observed = json.loads((observation / 'result.json').read_text())
    globals_ = observed.get('observed_globals', {})
    if (observed.get('uuid') != identity['uuid']
            or observed.get('kernel_sha256') != identity['kernel_sha256']
            or observed.get('development_no_ignition') is not True
            or observed.get('qemu_no_ignition') is not False
            or globals_.get('_sheng_development_no_ignition_enabled') != '0x1'
            or globals_.get('_sheng_qemu_no_ignition_enabled') != '0x0'
            or not int(observed.get('actual_commpage_dyld_flags', '0x0'), 16) & 8
            or observed.get('root_volume_uuid') != str(root_uuid)
            or observed.get('userland_started') is not True
            or observed.get('launchd_main_started') is not True):
        raise ValueError('requires matching development commpage and original launchd main execution')
    return observed


def qualify_isc_overlay(build, observation, identity, root_uuid):
    observed = qualify_development_dyld(build, observation, identity, root_uuid)
    values = observed.get('observed_globals', {})
    console = (observation / 'console.txt').read_text()
    recovery = json.loads((ROOT / 'config/macos26-recovery-root.json').read_text())
    mounts = ['mount-complete volume ' + name for name in recovery['isc']['volumes']]
    if any(marker not in console for marker in mounts) and (observation / 'stream-console.jsonl').exists():
        # A later ring tail may no longer contain successful early role mounts.
        # Accept their actual streamed bytes only with this snapshot's accounting.
        snapshot = json.loads((observation / 'gdb-stop.json').read_text())
        stream = snapshot.get('retained_log_stream', {})
        decoded = json.loads((observation / 'console.json').read_text())
        samples = [json.loads(line) for line in (observation / 'stream-console.jsonl').read_text().splitlines()]
        raw = (observation / 'stream-console.bin').read_bytes()
        expected_bytes = cursor = dropped = 0
        for sample in samples:
            position = cursor
            for chunk in sample['chunks']:
                if chunk['start'] != position or chunk['bytes'] <= 0:
                    raise ValueError('invalid retained ISC stream chunk extent')
                position += chunk['bytes']
            if (sample['start'] != cursor or sample['end'] < cursor or sample['missing_bytes'] != 0
                    or sample['guest_dropped_bytes'] < dropped
                    or position != sample['end']):
                raise ValueError('invalid retained ISC stream accounting')
            expected_bytes += sample['end'] - cursor
            if sample['guest_dropped_bytes'] > dropped:
                expected_bytes += len(f'\n[... {sample["guest_dropped_bytes"] - dropped} bytes dropped by guest writer ...]\n'.encode())
            cursor, dropped = sample['end'], sample['guest_dropped_bytes']
        if (snapshot.get('kernel_sha256') != identity['kernel_sha256'] or decoded.get('uuid') != identity['uuid']
                or stream.get('ready') is not True or stream.get('read_only') is not True
                or not 1 <= len(samples) <= 256 or stream.get('samples') != len(samples)
                or stream.get('total_bytes') != cursor or stream.get('missing_bytes') != 0
                or stream.get('guest_dropped_bytes') != dropped or len(raw) != expected_bytes):
            raise ValueError('requires this kernel snapshot and its actual retained ISC stream')
        console += '\n' + raw.decode('utf-8', errors='replace')
    if (values.get('_native_ufs_isc_overlay_enabled') != '0x1'
            or values.get('_native_ufs_isc_failures') != '0x0'
            or int(values.get('_native_ufs_isc_writes', '0x0'), 16) < 1
            or int(values.get('_native_ufs_isc_syncs', '0x0'), 16) < 1
            or int(values.get('_native_ufs_isc_resident_bytes', '0x0'), 16) > 64 << 20
            or int(values.get('_native_ufs_blocks_published', '0x0'), 16) != 2
            or any(marker not in console for marker in mounts)):
        raise ValueError('requires exact-candidate ISC, Preboot and Hardware mounts with real writes, syncs and no overlay failures')
    return observed


def qualify_macoslog(build, observation, readonly, deployment, identity):
    installed = json.loads(deployment.read_text())
    compiled = json.loads((build/'security-relink.json').read_text())['storage_driver']['build'].get('macoslog', {})
    sources = compiled.get('source_sha256', {})
    if (compiled.get('contract') != LOG_CONTRACT or compiled.get('default_off') is not True
            or compiled.get('writes_confined_to_preallocated_file') is not True
            or compiled.get('filesystem_metadata_writes') is not False
            or any(sources.get(name) != hashlib.sha256((ROOT/'platform/sheng/macos26/storage'/name).read_bytes()).hexdigest()
                   for name in ('macoslog_layout.h', 'macoslog_worker.h'))):
        raise ValueError('requires the current compiled default-off owned log file worker')
    first = installed.get('file_first_lba', 0)
    if (installed.get('status') != 'verified' or installed.get('partition') != LOG_LAYOUT['device']
            or installed.get('gpt_writes') is not False or installed.get('gpt_unchanged') is not True
            or installed.get('existing_allocations_preserved') is not True
            or not installed.get('image_sha256') or installed['image_sha256'] != installed.get('readback_sha256')
            or not LOG_LAYOUT['partition_first'] < first <= LOG_LAYOUT['partition_last'] + 1 - 512):
        raise ValueError('requires the verified preallocated macoslog file deployment')
    for folder, state in ((readonly, 2), (observation, 3)):
        observed = json.loads((folder/'result.json').read_text())
        backend = json.loads((folder/'macoslog-backend.json').read_text())
        values = observed.get('observed_globals', {})
        if (observed.get('kernel_sha256') != identity['kernel_sha256'] or observed.get('uuid') != identity['uuid']
                or backend.get('kernel_sha256') != identity['kernel_sha256'] or backend.get('uuid') != identity['uuid']
                or backend.get('schema') != 'sheng-macoslog-qemu-v1' or backend.get('transport_mocked') is not True
                or backend.get('production_worker_used') is not True or backend.get('outside_file_unchanged') is not True
                or backend.get('guard_unchanged') is not True or backend.get('writes_confined_to_owned_file') is not True
                or backend.get('image_before_sha256') != installed['image_sha256']
                or backend.get('file_first_lba') != first
                or values.get('_native_macoslog_state') != hex(state)
                or values.get('_native_macoslog_error') != '0x0'
                or values.get('_native_macoslog_first_lba') != hex(first)):
            raise ValueError('requires exact-candidate readonly and committed log observations')
        if state == 2:
            if (backend.get('write_calls') != 0 or backend.get('sync_calls') != 0
                    or values.get('_native_macoslog_snapshots') != '0x0'):
                raise ValueError('readonly log validation issued writes')
        else:
            if (int(values.get('_native_macoslog_snapshots', '0x0'), 16) < 2
                    or backend.get('write_calls', 0) < 4 or backend.get('sync_calls', 0) < 4):
                raise ValueError('requires two committed log banks and ordered synchronization')
            data = (folder/'macoslog.bin').read_bytes()
            _, decoded = decode_log_file(data, identity['uuid'])
            if (struct.unpack_from('<Q', data, len(data)-LOG_BLOCK+48)[0] != first
                    or decoded['generation'] != int(values.get('_native_macoslog_generation', '0x0'), 16)):
                raise ValueError('file readback generation differs from the stopped worker')
    return dict(file_first_lba=first, generation=decoded['generation'],
                hardware_write_qualified=False, full_history=False, final_panic_guaranteed=False)


def qualify_boot_progress(build):
    compiled = json.loads((build / 'build.json').read_text())
    progress = compiled.get('boot_progress', {})
    output = compiled.get('process_output_log', {})
    if progress.get('supported_cpus') == [1, 2, 4, 8]:
        from native_framebuffer import require_smp_owner
        require_smp_owner(compiled)
    if (progress.get('default_off') is not True or progress.get('stage_count') != 10
            or not (progress.get('required_cpus') == 1 or progress.get('supported_cpus') == [1, 2, 4, 8]) or progress.get('repeated_events_advance') is not False
            or progress.get('stalled_is_not_deadlock') is not True
            or output.get('default_off') is not True or output.get('vector_writes') is not True
            or output.get('max_bytes_per_write') != 159 or output.get('max_iovecs') != 16):
        raise ValueError('requires bounded default-off progress and process output diagnostics')
    for record, key, name in ((progress, 'model_sha256', 'boot_progress.h'),
                               (progress, 'kernel_sha256', 'boot_progress_kernel.h'),
                               (output, 'source_sha256', 'process_output.h'),
                               (compiled, 'retained_log_source_sha256', 'retained_log.h')):
        if record.get(key) != hashlib.sha256((ROOT / 'platform/sheng/macos26/log' / name).read_bytes()).hexdigest():
            raise ValueError('compiled boot progress or output source differs')
    for name in ('0055-native-process-output-log.patch', '0056-native-boot-progress.patch',
                 '0057-native-vector-output-log.patch', '0058-native-progress-clock-ready.patch',
                 '0059-native-progress-scheduler-header.patch', '0060-native-progress-thread-noreturn.patch'):
        path = ROOT / 'platform/sheng/macos26/patches' / name
        if compiled.get('patches', {}).get(str(path.relative_to(ROOT))) != hashlib.sha256(path.read_bytes()).hexdigest():
            raise ValueError('requires compiled service and vector-write hooks')
    return dict(enabled=True, source=progress, process_output_filter='WindowServer',
                process_output_mode=2, physical_qualified=False, runtime_qualified=False,
                progress_is_not_percent=True, silence_is_not_deadlock=True)


# CN sheng cores as read from the tablet (MIDR parts 0xd46/0xd4d/0xd47/0xd4e, MPIDR Affinity1 0x000..0x700).
SMP_CPU_REGS = ('0', '0x100', '0x200', '0x300', '0x400', '0x500', '0x600', '0x700')
SMP_CPU_COMPATIBLE = ('arm,cortex-a510',) * 3 + ('arm,cortex-a715',) * 2 + ('arm,cortex-a710',) * 2 + ('arm,cortex-x3',)


def hardware_smp_boot_args(boot_args: str) -> str:
    """Eight cores on the tablet: ArmCpu pends SPI 1008+core for IPIs (ICC_SGI1R_EL1 raises nothing on sheng)."""
    arguments = boot_args.split()
    if [item for item in arguments if item.startswith('cpus=')] != ['cpus=1']:
        raise ValueError('requires the single-CPU baseline boot arguments')
    arguments[arguments.index('cpus=1')] = 'cpus=8'
    return ' '.join(arguments + ['sheng-smp=1', 'armcpu-spi-ipi=1'])


def qualify_smp(build, observation, identity) -> dict:
    """The exact kernel must start all seven secondaries in QEMU; QEMU delivers IPIs as SGIs, not the board's SPIs."""
    from native_smp import configure
    compiled = json.loads((build / 'build.json').read_text())
    configure(compiled, 8, False, NATIVE_BOOT_ARGS)  # compiled secondary PAC key and reset-affinity fixes
    observed = json.loads((observation / 'result.json').read_text())
    runtime = observed.get('smp_runtime', {})
    if (observed.get('kernel_sha256') != identity['kernel_sha256'] or observed.get('uuid') != identity['uuid']
            or observed.get('reached') is not True or observed.get('smp_observation', {}).get('cpu_count') != 8
            or runtime.get('running_secondaries') != 7 or runtime.get('secondary_init_complete') is not True
            or runtime.get('failures') != [0] * 8 or runtime.get('psci') != [0] * 8
            or runtime.get('all_cores_received_ipi') is not True):
        raise ValueError('requires this exact kernel with all eight cores started in QEMU')
    return dict(cpu_count=8, cpu_regs=list(SMP_CPU_REGS), cpu_compatible=list(SMP_CPU_COMPATIBLE),
                psci_method='smc', hardware_ipi='software-pended SPI 1008+core (armcpu-spi-ipi=1)',
                qemu_ipi=observed['smp_observation'].get('ipi'), qemu_checkpoint=observed.get('checkpoint'),
                qemu_all_cores_received_timer=runtime.get('all_cores_received_timer'),
                hardware_qualified=False, scheduler_migration_qualified=False)


def parse_panel_size(text: str) -> tuple:
    try:
        width, height = (int(part) for part in text.lower().split('x'))
    except ValueError:
        raise argparse.ArgumentTypeError('expected WIDTHxHEIGHT in millimetres, e.g. 262x175') from None
    if not (50 <= width <= 1000 and 50 <= height <= 1000):
        raise argparse.ArgumentTypeError('panel size must be 50..1000 mm on each side')
    return width, height


def qualify_button_keys(build) -> dict:
    """The hardware key companion must be compiled into this kernel's storage driver under its reviewed contract."""
    from native_button_keys import validate
    driver = json.loads((build / 'security-relink.json').read_text())['storage_driver']
    record = driver.get('build', {}).get('native_button_keys')
    if not record or 'button_keys_vtable_abi' not in driver:
        raise ValueError('kernel lacks the audited hardware key companion')
    validate(record)
    return dict(record, runtime_qualified=False)


def display_copy_forward(panel_patch_sha256):
    """The reviewed always-on copy-forward step chained on the panel-size patch, or None (derive_hw_display_copy_forward.py)."""
    path = ROOT / 'platform/sheng/macos26/display/hw-copy-forward.json'
    record = json.loads(path.read_text()) if path.exists() else None
    if (record is None or record.get('schema') != 'sheng-hw-display-copy-forward-v1'
            or record.get('base_patch_sha256') != panel_patch_sha256 or record.get('always_on') is not True):
        return None
    return record


def qualify_panel_size(build, width_mm: int, height_mm: int) -> dict:
    """The compiled display patch must be the reviewed panel-size step (optionally followed by the copy-forward step);
    the boot arguments mirror its bounds."""
    panel = json.loads((ROOT / 'platform/sheng/macos26/display/hw-panel-size.json').read_text())
    driver = json.loads((build / 'security-relink.json').read_text())['storage_driver']
    compiled = driver.get('build', {}).get('native_gop_display', {}).get('patch_sha256')
    forward = display_copy_forward(panel.get('patch_sha256'))
    accepted = {panel.get('patch_sha256')} | ({forward['patch_sha256']} if forward else set())
    if panel.get('schema') != 'sheng-hw-display-panel-size-v1' or compiled not in accepted:
        raise ValueError('kernel display companion lacks the reviewed panel-size step')
    return dict(boot_arguments=[f'native-gop-width-mm={width_mm}', f'native-gop-height-mm={height_mm}'],
                width_mm=width_mm, height_mm=height_mm, patch_sha256=compiled,
                copy_forward=bool(forward and compiled == forward['patch_sha256']),
                aspect_checked_by_driver=True, hidpi_selection_qualified=False)


def qualify_display_keep_awake() -> dict:
    """The derived hardware switch must describe the exact display patch that qualify_language_selector binds."""
    display = ROOT / 'platform/sheng/macos26/display'
    record = json.loads((display / 'hw-keep-awake.json').read_text())
    patch = hashlib.sha256((display / '0001-native-gop-display.patch').read_bytes()).hexdigest()
    switch = record.get('hardware_switch', {})
    # The keep-awake patch may be followed by the reviewed default-off panel-size step (derive_hw_display_panel_size.py).
    panel_path = display / 'hw-panel-size.json'
    panel = json.loads(panel_path.read_text()) if panel_path.exists() else None
    panel_chained = (panel is not None and panel.get('schema') == 'sheng-hw-display-panel-size-v1'
                     and panel.get('base_patch_sha256') == record.get('patch_sha256') and panel.get('default_off') is True)
    # ... and that by the always-on copy-forward step (derive_hw_display_copy_forward.py).
    forward = display_copy_forward(panel.get('patch_sha256')) if panel_chained else None
    chained = panel_chained and (panel.get('patch_sha256') == patch or (forward is not None and forward['patch_sha256'] == patch))
    if (record.get('schema') != 'sheng-hw-display-keep-awake-v1' or not (record.get('patch_sha256') == patch or chained)
            or switch.get('boot_argument') != 'native-gop-keep-awake=1' or switch.get('default_off') is not True):
        raise ValueError('display patch lacks the reviewed hardware keep-awake switch')
    return dict(boot_argument=switch['boot_argument'], patch_sha256=patch, guard=switch['guard'],
                request='IOPMrootDomain::requestUserActive every 30 s', display_on_qualified=False,
                system_sleep_policy='ADT sleep-disabled (separate)')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kernel-build', type=Path, required=True)
    parser.add_argument('--qemu-observation', type=Path, required=True)
    parser.add_argument('--bootstrap-observation', type=Path, required=True)
    parser.add_argument('--default-off-observation', type=Path, required=True)
    parser.add_argument('--root-deployment', type=Path, required=True)
    parser.add_argument('--partition-plan', type=Path, required=True)
    parser.add_argument('--root-build', type=Path, required=True)
    parser.add_argument('--root-hash', type=Path, required=True)
    parser.add_argument('--root-volume-uuid', type=uuid.UUID, required=True)
    parser.add_argument('--firmware-build', type=Path, required=True)
    parser.add_argument('--backup-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--development-no-ignition', action='store_true')
    parser.add_argument('--isc-overlay', action='store_true',
                        help='expose the paired ISC seed through a bounded volatile RAM overlay')
    parser.add_argument('--dyld-observation', type=Path,
                        help='matching original launchd main execution with the explicit development flag')
    parser.add_argument('--macoslog-observation', type=Path,
                        help='matching native worker snapshot commits through an owned-file QEMU backend')
    parser.add_argument('--macoslog-readonly-observation', type=Path)
    parser.add_argument('--macoslog-deployment', type=Path)
    parser.add_argument('--recovery-ui-observation', type=Path,
                        help='audited same-kernel QEMU language selector and real input fixture')
    parser.add_argument('--boot-progress', action='store_true',
                        help='enable observed milestone footer, service events and bounded WindowServer output')
    parser.add_argument('--smp-observation', type=Path,
                        help='QEMU --smp 8 observation of this exact kernel; packages all eight sheng cores')
    parser.add_argument('--display-keep-awake', action='store_true',
                        help='send the original PM user-activity request every 30 s (native-gop-keep-awake=1); '
                             'the tablet has no input driver yet, so the display would otherwise idle off')
    parser.add_argument('--display-observation', type=Path,
                        help='display-only diagnostic: same-kernel QEMU fixture where WindowServer stays up and the GOP '
                             'companion submits a first frame; does not qualify language selection or input')
    parser.add_argument('--display-waiver', type=Path,
                        help='explicit user waiver of the complete same-kernel QEMU display window, bound to a partial '
                             'same-kernel display fixture; records the unobserved checks, qualifies nothing')
    parser.add_argument('--platform-uuid', type=uuid.UUID,
                        help='owned platform UUID for /options and the volatile NVRAM proxy (never an Apple identity)')
    parser.add_argument('--button-keys', action='store_true',
                        help='enable the compiled hardware key companion (native-button-keys=1): volume up/down = Up/Down, '
                             'power = Return, both volume = Tab; read-only SPMI polling, not qualified on hardware')
    parser.add_argument('--panel-size-mm', type=parse_panel_size,
                        help='WIDTHxHEIGHT physical panel size for the compiled panel-size display step, e.g. 262x175')
    args = parser.parse_args()
    gui = args.recovery_ui_observation or args.display_observation or args.display_waiver
    if args.display_keep_awake and not gui:
        parser.error('--display-keep-awake requires --recovery-ui-observation or --display-observation')
    if (args.button_keys or args.panel_size_mm) and not gui:
        parser.error('--button-keys and --panel-size-mm require the GOP display (UI qualification or display diagnostic)')
    if args.display_observation and args.recovery_ui_observation:
        parser.error('--display-observation is the display-only diagnostic; do not combine it with the UI qualification')
    if args.display_waiver and (args.display_observation or args.recovery_ui_observation):
        parser.error('--display-waiver replaces the display evidence; do not combine it with an observation')
    if args.platform_uuid is not None and args.platform_uuid.int == 0:
        parser.error('--platform-uuid cannot be nil')
    if args.development_no_ignition != bool(args.dyld_observation):
        parser.error('--development-no-ignition and --dyld-observation must be used together')
    if args.isc_overlay and not args.development_no_ignition:
        parser.error('--isc-overlay requires the qualified explicit development root')
    if gui and not args.isc_overlay:
        parser.error('Recovery UI and the display diagnostic require the qualified ISC development root')
    if args.boot_progress and not gui:
        parser.error('--boot-progress requires the qualified Recovery UI candidate or the display diagnostic')
    log_options = (args.macoslog_observation, args.macoslog_readonly_observation, args.macoslog_deployment)
    if any(log_options) and not all(log_options):
        parser.error('macoslog requires write, readonly and deployment evidence together')
    kernel, symbols, identity, media, partition = qualify(args.kernel_build,
        args.qemu_observation, args.bootstrap_observation, args.default_off_observation,
        args.root_deployment, args.partition_plan, args.root_build, with_isc=args.isc_overlay)
    root_hash = args.root_hash.read_bytes()
    hash_evidence = json.loads(args.root_hash.with_suffix(args.root_hash.suffix + '.json').read_text())
    root = json.loads(args.root_build.read_text())
    recovery = json.loads((ROOT / 'config/macos26-recovery-root.json').read_text())
    if args.root_volume_uuid != uuid.UUID(recovery['volume_uuid']):
        raise ValueError('root UUID differs from the compiled recovery transport')
    if (hash_evidence.get('build') != '25F71' or hash_evidence.get('manifest_digest_matches') is not True
            or hash_evidence.get('payload_sha256') != hashlib.sha256(root_hash).hexdigest()
            or hash_evidence.get('raw_disk_sha256') != root['windows'][0]['sha256']):
        raise ValueError('root-hash payload does not belong to the installed BaseSystem')
    firmware = json.loads(args.firmware_build.read_text())
    required = ['UniXNU/' + name for name in (
                '0007-native-16k-bootstrap-congruence', '0011-native-arm64e-loader',
                '0013-native-handoff-progress', '0014-native-handoff-code-coherency',
                '0015-native-retained-log-persistence', '0033-retained-log-high-bank-and-filesystem')]
    # The 12 GiB window needs the CN memory map, the raised DarwinBoot/ADT bound and the CN log saver.
    required += list(OWNED_MEMORY_FIRMWARE)
    for name in required:
        patch = ROOT / ('patches/' + name + '.patch')
        if firmware.get('patches_sha256', {}).get(str(patch.relative_to(ROOT))) != hashlib.sha256(patch.read_bytes()).hexdigest():
            raise ValueError('firmware lacks the pinned loader contract: ' + name)
    if firmware.get('preflight_only') is not False or firmware.get('native_el1') or firmware['bootimg']['compression'] != 'none':
        raise ValueError('requires the qualified normal sheng firmware container')
    base = ROOT / next(name for name in firmware['artifacts'] if name.endswith('.img'))
    base_data = base.read_bytes()
    if hashlib.sha256(base_data).hexdigest() != firmware['bootimg']['container_sha256']:
        raise ValueError('firmware image differs from its completed build')
    payload_size = struct.unpack_from('<I', base_data, 8)[0]
    payload = base_data[4096:4096 + payload_size]
    if len(payload) != payload_size:
        raise ValueError('firmware payload is truncated')
    stock_boot = args.backup_dir / 'boot_a.img'
    with stock_boot.open('rb') as stream:
        boot = stream.read(4096)
    geometry = tomllib.loads((ROOT / 'uefi-sheng/Resources/Configs/sheng.toml').read_text())['boot_image']
    os_version = stock_version_for_v0(boot, stock_boot.stat().st_size, geometry['header_version'])
    boot_args = NATIVE_BOOT_ARGS + ' -sheng-early-console -sheng-capture-handoff native-ufs-root=1 native-ufs-diagnostics=1'
    if args.development_no_ignition:
        qualify_development_dyld(args.kernel_build, args.dyld_observation, identity, args.root_volume_uuid)
        boot_args += ' -sheng-development-no-ignition'
    if args.isc_overlay:
        qualify_isc_overlay(args.kernel_build, args.dyld_observation, identity, args.root_volume_uuid)
        boot_args += ' native-ufs-isc-overlay=1'
    log_qualification = None
    if args.macoslog_observation:
        log_qualification = qualify_macoslog(args.kernel_build, args.macoslog_observation,
            args.macoslog_readonly_observation, args.macoslog_deployment, identity)
        boot_args += ' native-ufs-macoslog=2'
    ui_qualification = None
    if args.recovery_ui_observation:
        ui_qualification = qualify_language_selector(args.kernel_build, args.recovery_ui_observation, identity)
        boot_args += ' native-gop-display=1 sheng-recovery-ui=1'
    display_qualification = None
    if args.display_observation:
        display_qualification = qualify_display_observation(args.kernel_build, args.display_observation, identity)
        boot_args += ' ' + display_qualification['boot_argument']
    elif args.display_waiver:
        display_qualification = qualify_display_waiver(args.kernel_build, args.display_waiver, identity)
        boot_args += ' ' + display_qualification['boot_argument']
    gop_enabled = bool(ui_qualification or display_qualification)
    button_keys = None
    if args.button_keys:
        button_keys = qualify_button_keys(args.kernel_build)
        boot_args += ' ' + button_keys['boot_argument']
    panel_size = None
    if args.panel_size_mm:
        panel_size = qualify_panel_size(args.kernel_build, *args.panel_size_mm)
        boot_args += ' ' + ' '.join(panel_size['boot_arguments'])
    smp_qualification = None
    cpu_regs, cpu_compatible = ['0'], ['arm,cortex-a510']
    if args.smp_observation:
        smp_qualification = qualify_smp(args.kernel_build, args.smp_observation, identity)
        boot_args = hardware_smp_boot_args(boot_args)
        cpu_regs, cpu_compatible = list(SMP_CPU_REGS), list(SMP_CPU_COMPATIBLE)
    keep_awake = None
    if args.display_keep_awake:
        keep_awake = qualify_display_keep_awake()
        boot_args += ' ' + keep_awake['boot_argument']
    progress_qualification = None
    if args.boot_progress:
        progress_qualification = qualify_boot_progress(args.kernel_build)
        boot_args += ' sheng-boot-progress=1 sheng-stderr-log=2 sheng-stderr-name=WindowServer'
    args.output.mkdir(parents=True, exist_ok=False)
    try:
        with tempfile.TemporaryDirectory(dir=args.output) as temporary:
            work = Path(temporary)
            stage = work / 'EFI/UniXNU'
            files = stage / 'Payloads'
            files.mkdir(parents=True)
            shutil.copy2(kernel, files / 'kernel.macho')
            subprocess.run(['python3', ROOT / 'UniXNU-sheng/scripts/make-sheng-adt.py',
                '--output', files / 'kernel.macho.adt', '--dram-size', '0x200000000',
                '--owned-memory-size', hex(OWNED_MEMORY), '--panic-log', '--cpu-regs', ','.join(cpu_regs),
                *[part for compatible in cpu_compatible for part in ('--cpu-compatible', compatible)],
                '--random-seed-bytes', '256',
                '--os-environment', 'recoveryos',
                '--root-match-volume-uuid', str(args.root_volume_uuid)], check=True)
            configure_native_text_state(files / 'kernel.macho.adt', development_root=True,
                                        root_hash=root_hash, boot_volume_uuid=args.root_volume_uuid,
                                        platform_uuid=args.platform_uuid,
                                        native_gop_display=gop_enabled)
            transport_title = 'UFS+ISC' if args.isc_overlay else 'UFS'
            if log_qualification:
                transport_title += '+LOG'
            if ui_qualification:
                transport_title += '+UI'
            if display_qualification:
                transport_title += '+DISPLAY'
            (stage / 'unixnu.toml').write_text('version = 1\n[boot]\ntimeout = -1\nfallback_timeout = 30\ndefault = "native26ufs"\n'
                '[kits]\nautoload = false\n[[entry]]\nid = "native26ufs"\n'
                f'title = "SHENG macOS 26 - {transport_title} {identity["uuid"][:8]}"\ntype = "xnu"\n'
                'path = "Payloads/kernel.macho"\nargs = "' + boot_args + '"\n')
            content = sum(p.stat().st_size for p in stage.rglob('*') if p.is_file())
            mib = max(64, ((content * 5 // 4 + (1 << 20) - 1) >> 20) + 8)
            if mib > 192:
                raise ValueError('candidate exceeds the bounded RAM FAT budget')
            fat = work / 'native.fat'
            subprocess.run(['mformat', '-C', '-F', '-T', str(mib * 2048), '-v', 'SHENGXNU26',
                            '-N', '53484e26', '-i', fat, '::'], check=True)
            subprocess.run(['mcopy', '-i', fat, '-s', '-Q', work / 'EFI', '::/'], check=True)
            transport = work / 'Image-dtb'
            transport.write_bytes(gzip.compress(payload, mtime=0) + vendor_dtbs(args.backup_dir / 'vendor_boot_a.img'))
            image = work / 'sheng-macos26-kernel.img'
            command = ['python3', ROOT / 'uefi-sheng/Resources/Scripts/mkbootimg.py',
                       '--kernel', transport, '--ramdisk', fat, '-o', image]
            for name in ('base', 'kernel_offset', 'tags_offset', 'pagesize', 'header_version'):
                command += ['--' + name, str(geometry[name])]
            subprocess.run(command, check=True)
            data = bytearray(image.read_bytes())
            if (len(data) < 1632 or data[:8] != b'ANDROID!'
                    or struct.unpack_from('<I', data, 40)[0] != 0):
                raise ValueError('generated transport is not an Android v0 image')
            struct.pack_into('<I', data, 44, os_version)
            (args.output / image.name).write_bytes(data)
            shutil.copy2(files / 'kernel.macho.adt', args.output / 'kernel.macho.adt')
            record = dict(container_sha256=hashlib.sha256(data).hexdigest(), container_bytes=len(data),
                kernel=identity, purpose='Native EL1 UFS root experiment; hardware execution unqualified',
                stock_boot_metadata=dict(path=str(stock_boot.resolve()), header_version=4,
                    header_sha256=hashlib.sha256(boot).hexdigest(), image_bytes=stock_boot.stat().st_size,
                    os_version=os_version, os_version_preserved=True),
                transport_header_version=0, bootloader_acceptance_qualified=False,
                boot_args=boot_args, temporary_ram_boot=True, partition_images_written=False,
                root_media_preinstalled=True, root_partition_guid=partition['root_unique_guid'],
                root_image_sha256=media['sha256'], root_volume_uuid=str(args.root_volume_uuid),
                owned_memory_bytes=OWNED_MEMORY, native_panic_region_bytes=524288,
                source_crypto_post_passed=True, original_interfaces_qemu_registered=True,
                ufs_vtable_qualified=True, ufs_default_off_qemu_qualified=True,
                ufs_hardware_read_qualified=False, userland_started=False,
                qemu_observation=str(args.qemu_observation.resolve()),
                bootstrap_observation=str(args.bootstrap_observation.resolve()),
                default_off_observation=str(args.default_off_observation.resolve()),
                hardware_log_transport_qualified=False, firmware_sha256=firmware['sha256'],
                log_symbol_va=hex(symbols['_sheng_log_buffer']),
                firmware_log_partition=LOG_CONTRACT['partition_uuid'],
                firmware_log_repair=dict(ram_ranges=[[hex(a), hex(b)] for a, b in LOG_RAM_RANGES],
                    partition_bound_fat_connect=True, status_files=['ARM.BIN', 'RECOVERY.BIN'],
                    patch_sha256=firmware['patches_sha256']['patches/UniXNU/0033-retained-log-high-bank-and-filesystem.patch'],
                    layout_patch_sha256=firmware['patches_sha256']['patches/UniXNU/0042-native-log-saver-cn-layout.patch'],
                    warm_reboot_capture_qualified=False),
                development_root=True, synthetic_vm_identity=False, qemu_only_boot_args=False,
                development_no_ignition=args.development_no_ignition,
                isc_overlay=args.isc_overlay, isc_disk_writes=False,
                isc_overlay_persistent=False, isc_overlay_limit_bytes=(64 << 20) if args.isc_overlay else 0,
                macoslog=log_qualification,
                recovery_ui=ui_qualification, native_gop_display=gop_enabled,
                display_diagnostic=display_qualification, button_keys=button_keys, panel_size=panel_size,
                display_qualification_waived=bool(args.display_waiver),
                owned_platform_uuid=str(args.platform_uuid) if args.platform_uuid else None,
                display_keep_awake=keep_awake, smp=smp_qualification,
                boot_progress=progress_qualification,
                native_recovery_input=False, physical_input_qualified=False,
                dyld_observation=str(args.dyld_observation.resolve()) if args.dyld_observation else None,
                root_variant='recoveryos', recovery_transport_is_not_authentication=True,
                deployment_scope='fastboot boot only; no boot/recovery/UEFI partition flashing')
            (args.output / 'build.json').write_text(json.dumps(record, indent=2) + '\n')
            print(args.output / image.name)
    except BaseException as error:
        # Keep this attempt and any emitted artifacts distinct from a verified package.
        (args.output / 'failure.json').write_text(json.dumps(dict(
            status='failed', error_type=type(error).__name__, error=str(error)), indent=2) + '\n')
        raise


if __name__ == '__main__':
    main()
