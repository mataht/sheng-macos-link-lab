#!/usr/bin/env python3
"""Observe one admitted GOP start call in a fresh QEMU fixture; never reuse launchd PASS."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys

from observe_macos26_kernel import ROOT, identify
from native_macho import macho

GDB = ROOT / 'UniXNU/scripts/qemu-entry/gdb-stop.py'
FINISH = GDB.with_name('finish_function_observer.py')
INSTANCE = GDB.with_name('gop_instance_observer.py')
CACHE_READER = ROOT / 'scripts/native_user_cache_snapshot.py'
CACHE_LAYOUT_SHA = 'bd8a02455feffcadf0ac0a53bdb9c9433da1edec58f0bae636dd777b6c130055'
sys.path.insert(0, str(GDB.parent))
from finish_function_observer import kernel_entry_plan

START = '__ZN16NativeGOPDisplay5startEP9IOService'
EXPECTED = '7539890938ce3d93bda65246251932db0e2d240cfea8de58fe4339ad450d0aca'
INSTANCE_PRESETS = {'short': (90, 180), 'long': (600, 720)}
VALUES = {'--fixture', '--esp', '--kernel', '--firmware', '--gic-version', '--qemu-memory-mib',
    '--timeout', '--smp', '--primary-start-address', '--follow-entrypoint-arg', '--follow-main-entry-offset',
    '--also-stop', '--virtio-block-image', '--virtio-extra-block-image', '--native-mount-plan',
    '--native-mount-reader', '--allproc-address', '--kernel-va-bits', '--proc-pid-offset',
    '--proc-stat-offset', '--proc-list-offset', '--proc-comm-offset', '--native-thread-plan',
    '--native-thread-reader', '--read-memory', '--read-indirect'}
FLAGS = {'--system-registers', '--qemu-ramfb', '--capture-screen', '--continue-after-main',
         '--virtio-extra-block-overlay'}
DROP = {'--fixture', '--esp', '--kernel', '--timeout', '--primary-start-address',
        '--follow-entrypoint-arg', '--follow-main-entry-offset', '--continue-after-main', '--also-stop'}
GLOBALS = {'_disableConsoleOutput': 4, '_disable_iolog_serial_output': 1, '_sheng_framebuffer': 8}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, data):
    Path(path).write_text(json.dumps(data, indent=2) + '\n')


def source_options(command, finishing=False):
    if len(command) < 3 or Path(command[1]).resolve() != GDB.resolve():
        raise ValueError('source command is not the reviewed gdb-stop entry point')
    result, i = [], 3
    while i < len(command):
        flag = command[i]
        if (flag in VALUES or finishing and flag in {'--finish-instance-plan',
                '--native-user-cache-plan', '--native-user-cache-reader'}) and i + 1 < len(command):
            result.append((flag, command[i + 1])); i += 2
        elif flag in FLAGS or finishing and flag in {'--finish-function', '--finish-thread-match'}:
            result.append((flag, None)); i += 1
        else:
            raise ValueError('unreviewed source option: ' + flag)
    return result


def user_cache_plan(layout_path, kernel_build):
    from native_user_cache_snapshot import FIXED_NATIVE_GEOMETRY
    if sha(layout_path) != CACHE_LAYOUT_SHA:
        raise ValueError('requires the independently qualified fixed native cache layout')
    layout = json.loads(layout_path.read_text())
    process = json.loads((kernel_build / 'process-layout.json').read_text())
    if (layout['kernel_sha256'] != EXPECTED or process['kernel_sha256'] != EXPECTED
            or layout['threads'] != process['threads'] or layout['offsets'] != process['offsets']
            or layout['uuid'] != process['kernel_uuid'] or layout['va_bits'] != 47):
        raise ValueError('cache and process layouts do not match the paired candidate')
    directory = ROOT / 'out/windowserver-symbol-evidence/cache-files'
    map_path, = directory.rglob('dyld_shared_cache_arm64e.map')
    cache_path = map_path.with_suffix('')
    if (sha(cache_path) != 'a3a9892c1ff1f2464d76aac7c7936444f71fef470da9ad5b1d65699c7d8e5247'
            or sha(map_path) != 'ce94a65c95f2bfec80d1d3aca60cf6fba20453e9ef2c676d9d2636f75296e728'):
        raise ValueError('static cache identity differs from the verified local Recovery cache')
    cache = cache_path.read_bytes()
    mapping_offset, count = struct.unpack_from('<II', cache, 16)
    if not 0 < count <= 16 or not 104 <= mapping_offset <= len(cache) - count * 32:
        raise ValueError('invalid static cache mapping table')
    header_address, = struct.unpack_from('<Q', cache, mapping_offset)
    catalog_path = directory.parent / 'code-cache-catalog.json'
    catalog = json.loads(catalog_path.read_text())
    if (catalog['main_uuid'] != '7772aca1-5ce7-3e49-a63d-610819754e5c'
            or catalog['shared_region_start'] != header_address or catalog['header_extent_to_mappings'] != mapping_offset):
        raise ValueError('static cache catalog differs from its pinned header')
    ranges = [[int(a, 16), int(b, 16)] for a, b in re.findall(
        r'^mapping\s+EX\s+\S+\s+(0x[0-9A-Fa-f]+)\s+->\s+(0x[0-9A-Fa-f]+)$', map_path.read_text(), re.M)]
    if not ranges or any(a >= b for a, b in ranges):
        raise ValueError('static executable cache ranges are missing')
    return dict(schema='native-user-cache-plan-v1', kernel_sha256=EXPECTED, read_only=True,
        geometry_contract=FIXED_NATIVE_GEOMETRY,
        layout=layout, layout_sha256=sha(layout_path), process=process,
        cache=dict(magic_hex=cache[:16].hex(), uuid='7772aca1-5ce7-3e49-a63d-610819754e5c',
                   unslid_header_address=header_address, executable_ranges=ranges,
                   max_slide=catalog['max_slide'], catalog_sha256=sha(catalog_path),
                   static_cache_sha256=sha(cache_path), static_map_sha256=sha(map_path)))


def prepared_esp_evidence(esp, original):
    from prepare_macos26_qemu_ui import inspect_identity
    evidence_path, diff_path = esp.parent / 'identity-preparation.json', esp.parent / 'dt-diff.json'
    evidence, diff = json.loads(evidence_path.read_text()), json.loads(diff_path.read_text())
    actual = {str(p.relative_to(esp)): sha(p) for p in esp.rglob('*') if p.is_file()}
    original_hashes = {str(p.relative_to(original)): sha(p) for p in original.rglob('*') if p.is_file()}
    allowed = {('/device-tree/options', 'platform-uuid'), ('/device-tree/chosen', 'nvram-proxy-data')}
    identity = inspect_identity(esp / 'EFI/UniXNU/Payloads/kernel.macho.adt')
    if (actual != evidence['prepared_sha256'] or original_hashes != evidence['source_sha256']
            or evidence['changed_files'] != ['EFI/UniXNU/Payloads/kernel.macho.adt']
            or evidence['kernel_modified'] or evidence['apple_chip_identity_modified']
            or evidence['native_sleep_disabled'] or not evidence['qemu_only']
            or not evidence['synthetic_vm_identity'] or not evidence['source_hashes_unchanged']
            or identity is None or str(identity) != evidence['platform_uuid']
            or diff['node_paths_unchanged'] is not True or diff['only_expected_properties_changed'] is not True
            or {(d['path'], d['property']) for d in diff['differences']} != allowed):
        raise ValueError('prepared ESP is not the reviewed independent native VM UUID-only input')
    return dict(input_esp=str(esp), identity=evidence, dt_property_diff=diff,
        identity_metadata_sha256=sha(evidence_path), dt_diff_sha256=sha(diff_path),
        identity_readback=str(identity), readback_helper_sha256=sha(ROOT / 'scripts/prepare_macos26_qemu_ui.py'))


def prepare(args):
    window, timeout = INSTANCE_PRESETS[args.instance_preset]
    if args.timeout != timeout or args.instance_preset == 'long' and not args.instance_layout:
        raise ValueError('requires the fixed short 90/180 or instance-only long 600/720 preset')
    kernel, addresses, identity = identify(args.kernel_build.resolve())
    if identity['kernel_sha256'] != EXPECTED:
        raise ValueError('this bounded diagnostic is pinned to the admitted stage5 candidate')
    admission = json.loads((ROOT / 'out/stage5-apple-combined-probe/offline-admission.json').read_text())
    if admission['candidate_sha256'] != EXPECTED or admission.get('offline_checks_passed') is not True:
        raise ValueError('missing completed offline admission')
    components = json.loads((args.kernel_build / 'security-relink.json').read_text())
    driver = components['storage_driver']
    if (components['candidate_sha256'] != EXPECTED or driver['display_vtable_abi']['slots'] != 473
            or not driver['display_vtable_abi']['pac_metadata_matches']):
        raise ValueError('driver symbol map or display ABI differs from the admitted candidate')
    entry = driver['symbols'][START]
    entry_plan = kernel_entry_plan(kernel, entry)
    instance_plan = None
    if args.instance_layout:
        if not args.finish_function:
            raise ValueError('same-run instance snapshots require strict function return')
        layout = json.loads(args.instance_layout.read_text())
        build = driver['build']
        display = build['native_gop_display']
        if (layout.get('schema') != 'native-gop-instance-layout-v1' or layout.get('class_bytes') != 7504
                or layout.get('kernel_va_bits') != 47 or layout.get('read_only') is not True
                or layout.get('kernel_sha256') != EXPECTED or layout.get('driver_sha256') != build['binary_sha256']
                or layout.get('object_sha256') != sha(ROOT / 'out/ci-kernel/ufs-driver/NativeUfs.o')
                or layout.get('source_sha256') != display['source_sha256']
                or layout.get('header_sha256') != display['header_sha256'] or layout.get('paired_abi') != display['abi']
                or layout.get('vtable_address_point') != driver['symbols']['__ZTV16NativeGOPDisplay'] + 16):
            raise ValueError('instance layout does not match the admitted object, source and framebuffer ABI')
        required_guards = {'__ZN16NativeGOPDisplay4stopEP9IOService',
                           '__ZN16NativeGOPDisplayD0Ev', '__ZN16NativeGOPDisplayD1Ev'}
        if {item['symbol'] for item in layout['guards']} != required_guards:
            raise ValueError('instance lifetime guards are incomplete')
        for guard in layout['guards']:
            if (guard['address'] != driver['symbols'][guard['symbol']] or
                    kernel_entry_plan(kernel, guard['address'])['entry_bytes'] != guard['entry_bytes']):
                raise ValueError('instance lifecycle guard is not bound to actual executable code')
        for field in layout['fields'].values():
            if field['bytes'] not in (1, 4, 8) or not 0 <= field['offset'] <= 7504 - field['bytes']:
                raise ValueError('instance field is outside the compiler-qualified extent')
        natural = kernel_entry_plan(kernel, addresses['_sleh_synchronous'])
        instance_plan = dict(schema='native-gop-instance-plan-v1', layout=layout,
            layout_sha256=sha(args.instance_layout), kernel_sha256=EXPECTED,
            natural_el1=dict(address=addresses['_sleh_synchronous'], entry_bytes=natural['entry_bytes']),
            halt_address=addresses['_halt_all_cpus'], window_seconds=window, total_timeout=timeout)
    source = args.source_fixture.resolve()
    prior = json.loads((source / 'result.json').read_text())
    if (prior['kernel_sha256'] != EXPECTED or not prior.get('native_gop_display')
            or not prior.get('launchd_main_started') or not prior.get('isc_format_input')):
        raise ValueError('requires the completed stage5 GOP/launchd fixture with isolated ISC input')
    esp = source / 'esp'
    esp_evidence = None
    if args.prepared_esp:
        esp = args.prepared_esp.resolve()
        esp_evidence = prepared_esp_evidence(esp, source / 'esp')
    if sha(esp / 'EFI/UniXNU/Payloads/kernel.macho') != EXPECTED:
        raise ValueError('source ESP kernel differs from admitted candidate')
    config = (esp / 'EFI/UniXNU/unixnu.toml').read_text()
    if ('native-gop-display=1' not in config or 'sheng-boot-progress=' in config
            or 'sheng-early-console' in config):
        raise ValueError('source fixture must retain the reviewed GOP-only boot settings')
    command = json.loads((source / 'command.json').read_text())
    options = source_options(command)
    if ('--virtio-extra-block-overlay', None) not in options or ('--smp', '1') not in options:
        raise ValueError('requires the single-CPU fixture and a new isolated ISC overlay')
    map_text = (args.kernel_build / 'kernel.development.sheng.map').read_text(errors='replace')
    widths = {}
    globals_requested = dict(GLOBALS)
    if esp_evidence:
        globals_requested['_gIOPlatformUUIDAndSerialDone'] = 1
    for name, width in globals_requested.items():
        if name == '_sheng_framebuffer':
            widths[name] = dict(address=addresses[name], bytes=width, type='owner:uint32 + writer:uint32')
            continue
        found = re.findall(r'^0x([0-9A-Fa-f]+)\s+0x([0-9A-Fa-f]+)\s+\[[^]]+\]\s+' + re.escape(name) + r'$', map_text, re.M)
        expected_extent = 8 if name == '_gIOPlatformUUIDAndSerialDone' else width
        if len(found) != 1 or int(found[0][0], 16) != addresses[name] or int(found[0][1], 16) != expected_extent:
            raise ValueError('log switch address/width differs from the paired link map')
        widths[name] = dict(address=addresses[name], bytes=width,
            type='int' if width == 4 else '_Bool',
            dwarf_type_die='0x007ceff1' if width == 4 else '0x0229b7b0',
            source='matching native DWARF type byte_size and link-map extent')
        if name == '_gIOPlatformUUIDAndSerialDone':
            widths[name].update(type='bool', dwarf_type_die='0x01f06a3c',
                source='DWARF variable 0x01f19594 -> bool byte_size 1; link-map extent 8 includes padding')
    cache_plan = user_cache_plan(args.user_cache_layout, args.kernel_build) if args.user_cache_layout else None
    if cache_plan and not instance_plan:
        raise ValueError('user cache sampling requires the reviewed final-EL1 instance observation')
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    shutil.copytree(esp, out / 'esp')
    new = [sys.executable, str(GDB), hex(entry), '--fixture', str(out), '--esp', str(out / 'esp'),
           '--kernel', str(kernel.resolve()), '--timeout', str(timeout)]
    old_prefix = f'{int(command[2], 16):x}:10'
    for flag, value in options:
        if flag in DROP or flag == '--read-memory' and value == old_prefix:
            continue
        new.append(flag)
        if value is not None:
            new.append(value)
    new += ['--also-stop', hex(addresses['_halt_all_cpus']), '--read-memory', f'{entry:x}:10']
    for name, width in globals_requested.items():
        request = f'{addresses[name]:x}:{width:x}'
        if request not in new:
            new += ['--read-memory', request]
    if args.finish_function:
        new += ['--finish-function', '--finish-thread-match']
    if instance_plan:
        write(out / 'instance-plan.json', instance_plan)
        new += ['--finish-instance-plan', str(out / 'instance-plan.json')]
    if cache_plan:
        write(out / 'user-cache-plan.json', cache_plan)
        new += ['--native-user-cache-plan', str(out / 'user-cache-plan.json'),
                '--native-user-cache-reader', str(CACHE_READER)]
    manifest = {str(p.relative_to(out)): sha(p) for p in (out / 'esp').rglob('*') if p.is_file()}
    plan = dict(schema='native-gop-start-observation-v1', kernel=str(kernel.resolve()), kernel_sha256=EXPECTED,
        kernel_uuid=identity['uuid'], source_fixture=str(source), output=str(out), timeout=timeout,
        symbol=START, entry_plan=entry_plan, finish_function=args.finish_function,
        globals=widths, halt_address=hex(addresses['_halt_all_cpus']), command=new,
        gdb_sha256=sha(GDB), finish_helper_sha256=sha(FINISH), runner_sha256=sha(__file__), esp_sha256=manifest,
        initial_guest_state_modified=False, new_isc_overlay_required=True, launchd_observation=False)
    if instance_plan:
        plan.update(instance_plan_sha256=sha(out / 'instance-plan.json'),
                    instance_helper_sha256=sha(INSTANCE), instance_window_seconds=window)
    if cache_plan:
        plan.update(user_cache_plan_sha256=sha(out / 'user-cache-plan.json'),
                    user_cache_reader_sha256=sha(CACHE_READER), user_cache_sample_at_final_stop=True)
    if esp_evidence:
        write(out / 'prepared-esp-evidence.json', esp_evidence)
        plan.update(prepared_esp_evidence_sha256=sha(out / 'prepared-esp-evidence.json'),
                    prepared_esp_platform_uuid=esp_evidence['identity_readback'],
                    initial_guest_configuration_changed=True, initial_guest_state_modified=True,
                    guest_configuration_change='owned QEMU platform UUID in independent ESP only')
    write(out / 'diagnostic-plan.json', plan)
    write(out / 'command.json', new)
    print(json.dumps(dict(prepared=str(out / 'diagnostic-plan.json'), entry=hex(entry), timeout=timeout,
        finish_function=args.finish_function, qemu_executed=False)))
    return out / 'diagnostic-plan.json'


def decode(plan):
    out = Path(plan['output'])
    snapshot = json.loads((out / 'gdb-stop.json').read_text())
    if snapshot['kernel_sha256'] != plan['kernel_sha256']:
        raise ValueError('snapshot belongs to another kernel')
    entry_address = plan['entry_plan']['entry']
    pstate = int(snapshot['pstate'], 16)
    el1 = pstate & 15 in (4, 5)
    timed_out = snapshot['bounded_timeout_interrupted']
    instruction_match = snapshot.get('memory', {}).get(f'{entry_address:x}:10') == plan['entry_plan']['entry_bytes']
    strict = snapshot.get('strict_function_observation', {})
    entry = strict.get('entry')
    if plan['finish_function']:
        entry_seen = bool(entry and entry['pc'] == hex(entry_address) and entry['non_timeout']
            and entry['code_matches_candidate'] and entry['entry_bytes'] == plan['entry_plan']['entry_bytes'])
        returned_evidence = strict.get('returned') or {}
        if plan.get('instance_window_seconds'):
            returned = bool(entry_seen and strict.get('kernel_sha256') == EXPECTED and strict.get('return_verified')
                and returned_evidence.get('el1') and returned_evidence.get('non_timeout')
                and returned_evidence.get('register_snapshot_matched'))
        else:
            returned = bool(entry_seen and strict.get('kernel_sha256') == EXPECTED and strict.get('return_verified')
                and instruction_match and el1 and not timed_out)
    else:
        entry_seen = bool(int(snapshot['pc'], 16) == entry_address and instruction_match and el1 and not timed_out)
        returned = False
    raw_x0 = int(returned_evidence.get('raw_x0', snapshot['x0']) if plan.get('instance_window_seconds')
                 else snapshot['x0'], 16)
    w0 = raw_x0 & 0xffffffff
    returned_bool = bool(w0) if returned and w0 in (0, 1) else None
    status = ('returned_true' if returned_bool is True else 'returned_false' if returned_bool is False
        else 'invalid_bool_return' if returned else 'entry_without_verified_return' if entry_seen
        else 'no_entry')
    globals_seen = {}
    for name, field in plan['globals'].items():
        raw = snapshot.get('memory', {}).get(f'{field["address"]:x}:{field["bytes"]:x}', '')
        valid = len(raw) == field['bytes'] * 2 and not raw.startswith('E')
        try:
            value = int.from_bytes(bytes.fromhex(raw), 'little') if valid else None
        except ValueError:
            valid, value = False, None
        globals_seen[name] = dict(**field, raw=raw, validated=valid, value=value)
    framebuffer = globals_seen['_sheng_framebuffer']['value']
    result = dict(schema=plan['schema'], kernel_sha256=EXPECTED, symbol=plan['symbol'], status=status,
        diagnostic_capture_completed=True, entry_observed=entry_seen, return_observed=returned,
        returned_bool=returned_bool, raw_x0=hex(raw_x0), w0=hex(w0),
        return_el1=bool(returned_evidence.get('el1')) if plan.get('instance_window_seconds') else el1,
        timeout=timed_out, stopped_at_halt=snapshot['pc'] == plan['halt_address'],
        entry_evidence=entry, return_evidence=strict.get('returned'),
        instruction_bytes_match=instruction_match, globals=globals_seen,
        framebuffer_owner=framebuffer & 0xffffffff if framebuffer is not None else None,
        framebuffer_writer=framebuffer >> 32 if framebuffer is not None else None,
        proves_completed_frame=False, launchd_main_observed_in_this_run=False,
        guest_registers_or_memory_modified=False, hardware_admitted=False)
    if plan.get('instance_window_seconds'):
        result.update(instance_observation=snapshot.get('gop_instance_observation'),
            final_snapshot_pstate=snapshot['pstate'], final_snapshot_el1=el1,
            start_return_preserved_independently=True)
    if plan.get('user_cache_sample_at_final_stop'):
        result['user_cache_observation'] = snapshot.get('native_user_cache_snapshot')
    if plan.get('prepared_esp_platform_uuid'):
        result.update(prepared_vm_platform_uuid=plan['prepared_esp_platform_uuid'],
            platform_uuid_serial_done=globals_seen['_gIOPlatformUUIDAndSerialDone']['value'],
            identity_configuration_proves_fix=False)
    write(out / 'driver-start.json', result)
    print(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kernel-build', type=Path)
    parser.add_argument('--source-fixture', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--timeout', type=int, default=180)
    parser.add_argument('--finish-function', action='store_true')
    parser.add_argument('--instance-layout', type=Path,
                        help='qualified same-run instance snapshot layout')
    parser.add_argument('--instance-preset', choices=tuple(INSTANCE_PRESETS), default='short',
                        help='fixed window/deadline pair: short 90/180 (default), long 600/720')
    parser.add_argument('--user-cache-layout', type=Path, help='Opt-in paired WindowServer final-stop cache layout')
    parser.add_argument('--prepared-esp', type=Path, help='Independent reviewed ESP with explicit native VM UUID provenance')
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--run-prepared', type=Path)
    args = parser.parse_args()
    if not args.run_prepared:
        if not all((args.kernel_build, args.source_fixture, args.output)):
            parser.error('preparation requires --kernel-build, --source-fixture and --output')
        plan_path = prepare(args)
        if args.prepare_only:
            return
    else:
        plan_path = args.run_prepared
    plan = json.loads(plan_path.read_text())
    out = Path(plan['output'])
    if plan.get('instance_window_seconds'):
        instance_plan = json.loads((out / 'instance-plan.json').read_text())
        if ((plan['instance_window_seconds'], plan['timeout']) not in INSTANCE_PRESETS.values()
                or instance_plan['window_seconds'] != plan['instance_window_seconds']
                or instance_plan['total_timeout'] != plan['timeout']
                or not plan['finish_function']):
            raise ValueError('prepared instance preset is not one of the two fixed pairs')
    elif plan['timeout'] != 180:
        raise ValueError('the non-instance diagnostic retains its 180-second deadline')
    runtime_options = source_options(plan['command'], finishing=True)
    if (plan['command'][0] != sys.executable or int(plan['command'][2], 16) != plan['entry_plan']['entry']
            or ('--fixture', str(out)) not in runtime_options
            or ('--esp', str(out / 'esp')) not in runtime_options
            or ('--kernel', plan['kernel']) not in runtime_options
            or ('--timeout', str(plan['timeout'])) not in runtime_options
            or (plan.get('instance_window_seconds') and
                ('--finish-instance-plan', str(out / 'instance-plan.json')) not in runtime_options)
            or (plan.get('user_cache_sample_at_final_stop') and not all(item in runtime_options for item in (
                ('--native-user-cache-plan', str(out / 'user-cache-plan.json')),
                ('--native-user-cache-reader', str(CACHE_READER)))))
            or ('--kernel-va-bits', '47') not in runtime_options
            or ('--virtio-extra-block-overlay', None) not in runtime_options
            or any(flag in {'--primary-start-address', '--follow-entrypoint-arg',
                           '--follow-main-entry-offset', '--continue-after-main'} for flag, _ in runtime_options)
            or (plan['finish_function'] and not all((flag, None) in runtime_options
                for flag in ('--finish-function', '--finish-thread-match')))):
        raise ValueError('prepared command differs from the bounded driver-only observation')
    if (plan['schema'] != 'native-gop-start-observation-v1' or plan['kernel_sha256'] != EXPECTED
            or sha(plan['kernel']) != EXPECTED or sha(GDB) != plan['gdb_sha256']
            or sha(FINISH) != plan['finish_helper_sha256'] or sha(__file__) != plan['runner_sha256']
            or (plan.get('instance_window_seconds') and (sha(INSTANCE) != plan['instance_helper_sha256']
                or sha(out / 'instance-plan.json') != plan['instance_plan_sha256']))
            or (plan.get('user_cache_sample_at_final_stop') and (sha(CACHE_READER) != plan['user_cache_reader_sha256']
                or sha(out / 'user-cache-plan.json') != plan['user_cache_plan_sha256']))
            or (plan.get('prepared_esp_evidence_sha256') and
                sha(out / 'prepared-esp-evidence.json') != plan['prepared_esp_evidence_sha256'])
            or any(sha(out / name) != digest for name, digest in plan['esp_sha256'].items())
            or any((out / name).exists() for name in ('isc-overlay.qcow2', 'vars.fd', 'gdb-stop.json', 'run-started'))):
        raise ValueError('prepared diagnostic changed or runtime state already exists; use a fresh fixture')
    (out / 'run-started').write_text('one bounded QEMU diagnostic\n')
    with (out / 'observer.log').open('w') as log:
        completed = subprocess.run(plan['command'], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    if completed.returncode:
        raise RuntimeError('gdb-stop failed; inspect the retained observer.log; no return is qualified')
    decode(plan)


if __name__ == '__main__':
    main()
