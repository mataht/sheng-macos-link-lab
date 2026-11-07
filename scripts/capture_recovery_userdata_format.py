#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Qualify saved, read-only stock Recovery observations; never connect to or write a device."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import uuid


SCHEMA = 'sheng-stock-recovery-filesystem-v1'
# Extracted from the verified CN OS3.0.306 recovery_a.img backup (SHA-256 6262172299c1a4b275d262fcead0b72f27a2775cae345df1e0c3e9ae0b646bce).
RECOVERY_SHA256 = 'ea90876ea68cc534ab72fe273b7e1d2b845b61eba3263346e06d070778431857'
FSTAB_SHA256 = '176328ecdc5f0d49a86c46b617df1d6084b5715bbc253521781abad54a2a35f3'
COMMANDS = {
    'state': ['get-state'],
    **{key: ['shell', command] for key, command in {
        'uid': 'id -u', 'device': 'getprop ro.product.device', 'serial': 'getprop ro.serialno',
        'twrp': 'getprop ro.twrp.version', 'bootmode': 'getprop ro.bootmode',
        'fingerprint': 'getprop ro.build.fingerprint',
        'boot_id': 'cat /proc/sys/kernel/random/boot_id', 'uptime': 'cat /proc/uptime',
        'mounts': 'cat /proc/mounts',
        'userdata_node': 'readlink -f /dev/block/bootdevice/by-name/userdata',
        'userdata_start': 'cat /sys/class/block/sda29/start',
        'userdata_size': 'cat /sys/class/block/sda29/size',
        'userdata_uevent': 'cat /sys/class/block/sda29/uevent',
        'root_node': 'readlink -f /dev/block/by-name/macosroot',
        'root_start': 'cat /sys/class/block/sda31/start',
        'root_size': 'cat /sys/class/block/sda31/size',
        'root_uevent': 'cat /sys/class/block/sda31/uevent',
        'statfs': 'stat -f -c "%S %b" /data', 'df': 'df -k /data',
        'recovery_binary_sha256': 'sha256sum /system/bin/recovery',
        'recovery_fstab_sha256': 'sha256sum /system/etc/recovery.fstab',
        'uptime_end': 'cat /proc/uptime', 'boot_id_end': 'cat /proc/sys/kernel/random/boot_id',
    }.items()},
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def decimal(value):
    if not re.fullmatch(r'[0-9]+', value):
        raise ValueError('requires complete decimal output')
    return int(value)


def uevent(value):
    entries = [line.split('=', 1) for line in value.splitlines()]
    if any(len(pair) != 2 for pair in entries) or len({pair[0] for pair in entries}) != len(entries):
        raise ValueError('requires unambiguous kernel partition identity')
    return dict(entries)


def _parse(serial, plan_bytes, raw_bytes):
    plan, raw = json.loads(plan_bytes), json.loads(raw_bytes)
    plan_sha256 = hashlib.sha256(plan_bytes).hexdigest()
    if (plan.get('layout') != 'sheng-cn-1tb-lun0' or plan.get('disk') != 'sda'
            or plan.get('logical_block_bytes') != 4096 or plan.get('root_index') != 31
            or plan.get('root_name') != 'macosroot' or plan.get('applied') is not True
            or plan.get('gpt_readback_verified') is not True or plan.get('serial') != serial
            or raw.get('schema') != 'sheng-stock-recovery-raw-v1' or raw.get('serial') != serial
            or raw.get('device_writes') is not False or raw.get('plan_sha256') != plan_sha256
            or canonical(raw.get('applied_plan')) != canonical(plan)):
        raise ValueError('requires stock Recovery observations bound to the applied CN plan')
    userdata = plan['userdata']
    if (userdata['index'] != 29 or userdata['bytes'] != plan['userdata_bytes']
            or (userdata['last_lba'] - userdata['first_lba'] + 1) * 4096 != plan['userdata_bytes']
            or (plan['root_last_lba'] - plan['root_first_lba'] + 1) * 4096 != plan['root_bytes']):
        raise ValueError('inconsistent applied partition extents')
    uuid.UUID(plan['root_unique_guid'])
    outputs = raw['outputs']
    if set(outputs) != set(COMMANDS):
        raise ValueError('requires the complete stock Recovery capture command set')
    values = {}
    for key, command in COMMANDS.items():
        result = outputs[key]
        if (result.get('command') != command or type(result.get('returncode')) is not int
                or result['returncode'] != 0 or not isinstance(result.get('stdout'), str)
                or not isinstance(result.get('stderr'), str)):
            raise ValueError('failed or mismatched stock Recovery observation: ' + key)
        values[key] = result['stdout'].strip()
    # ro.bootmode is actually "unknown" on this stock Recovery. Do not invent an Android/setup-wizard state.
    if (values['state'] != 'recovery' or values['uid'] != '2000' or values['device'] != 'sheng'
            or values['serial'] != serial or values['twrp']
            or values['recovery_binary_sha256'].split() != [RECOVERY_SHA256, '/system/bin/recovery']
            or values['recovery_fstab_sha256'].split() != [FSTAB_SHA256, '/system/etc/recovery.fstab']):
        raise ValueError('requires the observed stock sheng Recovery identity')
    boot_id = str(uuid.UUID(values['boot_id']))
    if values['boot_id'] != boot_id or values['boot_id_end'] != boot_id:
        raise ValueError('Recovery boot changed during capture')
    uptime = []
    for key in ('uptime', 'uptime_end'):
        if not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?\s+[0-9]+(?:\.[0-9]+)?', values[key]):
            raise ValueError('invalid Recovery uptime')
        uptime.append(float(values[key].split()[0]))
    start, end = (datetime.fromisoformat(raw[key]) for key in ('captured_start_utc', 'captured_end_utc'))
    if start.utcoffset() is None or end.utcoffset() is None or end < start or uptime[1] < uptime[0]:
        raise ValueError('inconsistent capture interval')
    report = raw['user_reported_wipe_completed']
    if report.get('value') is not True or not report.get('source') or not report.get('text'):
        raise ValueError('requires separately attributed user report of completed wipe')
    data_mounts = [line.split() for line in values['mounts'].splitlines()
                   if len(line.split()) >= 3 and line.split()[1] == '/data']
    # The captured readlink command resolves this exact mount-source alias, not an unrelated by-name path.
    if (len(data_mounts) != 1 or data_mounts[0][0] != '/dev/block/bootdevice/by-name/userdata'
            or data_mounts[0][2] != 'f2fs' or values['userdata_node'] != '/dev/block/sda29'
            or values['root_node'] != '/dev/block/sda31'):
        raise ValueError('requires direct userdata F2FS with the captured source resolving to sda29')
    for prefix, index, name, first, size in (
            ('userdata', 29, 'userdata', userdata['first_lba'], plan['userdata_bytes']),
            ('root', 31, 'macosroot', plan['root_first_lba'], plan['root_bytes'])):
        identity = uevent(values[prefix + '_uevent'])
        if (identity.get('DEVNAME') != 'sda' + str(index) or identity.get('DEVTYPE') != 'partition'
                or identity.get('PARTN') != str(index) or identity.get('PARTNAME') != name
                or decimal(values[prefix + '_start']) * 512 != first * 4096
                or decimal(values[prefix + '_size']) * 512 != size):
            raise ValueError('kernel ' + name + ' identity or extent differs from the applied plan')
    statfs = values['statfs'].split()
    if len(statfs) != 2:
        raise ValueError('requires statfs block size and total blocks')
    block_size, blocks = map(decimal, statfs)
    filesystem_bytes = block_size * blocks
    if block_size != 4096 or not (16 << 30) <= filesystem_bytes <= plan['userdata_bytes']:
        raise ValueError('Recovery filesystem exceeds the resized userdata boundary or is too small')
    return dict(schema=SCHEMA, serial=serial, read_only=True, device_writes=False,
                evidence_kind='stock-recovery-filesystem', plan_sha256=plan_sha256, boot_id=boot_id,
                captured_start_utc=raw['captured_start_utc'], captured_end_utc=raw['captured_end_utc'],
                recovery_binary_sha256=RECOVERY_SHA256, recovery_fstab_sha256=FSTAB_SHA256,
                filesystem='f2fs', filesystem_bytes=filesystem_bytes,
                mount_source=data_mounts[0][0], physical_partition='sda29', physical_disk='sda',
                userdata_partition_bytes=plan['userdata_bytes'], root_partition_guid=plan['root_unique_guid'],
                root_partition_guid_source='applied plan; kernel macosroot name and extent matched',
                recovery_wipe_log_observed=False, user_reported_wipe_completed=report,
                android_initialization_observed=False, format_pending_vold=True,
                raw_capture_sha256=hashlib.sha256(raw_bytes).hexdigest(),
                raw_capture_encoding='utf-8 JSON containing subprocess text outputs and return codes',
                raw_capture_utf8=raw_bytes.decode('utf-8'))


def parse(serial, plan_bytes, raw_bytes):
    try:
        return _parse(serial, plan_bytes, raw_bytes)
    except (KeyError, TypeError, AttributeError, UnicodeError) as exc:
        raise ValueError('invalid stock Recovery filesystem evidence') from exc


def qualify_recovery_format(evidence, plan_bytes, serial):
    try:
        expected = parse(serial, plan_bytes, evidence['raw_capture_utf8'].encode('utf-8'))
        if canonical(evidence) != canonical(expected):
            raise ValueError('Recovery derived fields differ from the embedded raw observations')
    except (KeyError, TypeError, AttributeError, UnicodeError) as exc:
        raise ValueError('invalid stock Recovery filesystem evidence') from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial', required=True)
    parser.add_argument('--partition-plan', required=True, type=Path)
    parser.add_argument('--raw-input', required=True, type=Path, help='saved stock Recovery command results JSON')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    evidence = parse(args.serial, (args.partition_plan / 'plan.json').read_bytes(), args.raw_input.read_bytes())
    # Exclusive creation preserves both the original capture and any earlier evidence output.
    with args.output.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(json.dumps(evidence, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps({key: value for key, value in evidence.items() if key != 'raw_capture_utf8'}, indent=2))


if __name__ == '__main__':
    main()
