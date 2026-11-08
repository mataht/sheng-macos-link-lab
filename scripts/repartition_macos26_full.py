#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Plan or apply GPT entry 32 (macossystem) for the full-install container on the CN 1 TB sheng LUN0.

Starts from the verified Step-4 layout (userdata 128 GiB, macoslog, 32 GiB macosroot, unallocated tail).
Only the empty, last GPT entry is filled with a new partition in the unallocated tail; every other entry,
userdata contents, macoslog and macosroot stay untouched. Planning never writes. Applying writes only the two
GPT entry arrays and headers, each with readback, in root Recovery after explicit user approval.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import struct
import uuid

from backup_device import parse_gpt
from create_log_partition import geometry, update
import repartition_sheng_cn_1tb as cn

FULL_INDEX = 31                 # GPT entry 32, the last free slot of the 32-entry LUN0 array
FULL_NAME = 'macossystem'
MINIMUM_BYTES = 16 << 30
STEP4_LAYOUT = cn.PRESERVED     # verified after-GPT copies saved by the Step-4 apply
PRESERVED_FULL = cn.ROOT / 'private/backups/sheng-cn-1tb-lun0-full-layout'


def layout_full(primary, secondary, lbas, size_bytes, full_guid=None):
    if lbas != cn.LUN0_LBAS:
        raise ValueError('requires the reviewed CN 1 TB LUN0 capacity')
    if size_bytes < MINIMUM_BYTES or size_bytes % cn.WINDOW:
        raise ValueError('partition size must be at least 16 GiB and a whole number of MiB')
    before, header, entries, _, count, stride = geometry(primary, lbas, 0)
    other = geometry(secondary, lbas, lbas - cn.ALIGN)
    if (entries != other[2] or header[56:72] != other[1][56:72] or before['partitions'] != other[0]['partitions']
            or count != 32 or stride != 128):
        raise ValueError('primary and secondary GPT copies disagree or have an unexpected entry array')
    slot = lambda i: entries[i * stride:(i + 1) * stride]
    named = lambda i: slot(i)[56:128].decode('utf-16-le').split('\0')[0]
    if (named(cn.USERDATA_INDEX), named(cn.LOG_INDEX), named(cn.ROOT_INDEX)) != ('userdata', 'macoslog', 'macosroot'):
        raise ValueError('requires the applied userdata/macoslog/macosroot layout in entries 29-31')
    if slot(FULL_INDEX) != bytes(stride) or any(p['name'] == FULL_NAME for p in before['partitions']):
        raise ValueError('GPT entry 32 must be completely empty and no macossystem may exist')
    root_first, root_last = struct.unpack_from('<QQ', slot(cn.ROOT_INDEX), 32)
    if any(p['last_lba'] > root_last for p in before['partitions']) or root_last >= before['last_usable']:
        raise ValueError('macosroot must be the final allocated partition, followed by free space')
    first = cn.up(root_last + 1)
    last = first + size_bytes // cn.BLOCK - 1
    if last > before['last_usable']:
        raise ValueError('requested macossystem does not fit in the unallocated tail')
    full_guid = full_guid or uuid.uuid4()
    if full_guid.int == 0:
        raise ValueError('macossystem identity must be nonzero')
    entry = cn.entry_bytes(FULL_NAME, first, last, full_guid, stride)
    outputs = {}
    for name, raw, start in (('primary', primary, 0), ('secondary', secondary, lbas - cn.ALIGN)):
        changed = update(raw, lbas, start, FULL_INDEX, entry)
        after = geometry(changed, lbas, start)
        for i in range(count):
            if after[2][i * stride:(i + 1) * stride] != (entry if i == FULL_INDEX else slot(i)):
                raise ValueError('unexpected GPT entry change')
        if after[1][24:56] != (header if start == 0 else other[1])[24:56] or after[1][56:72] != header[56:72]:
            raise ValueError('GPT header geometry or disk identity changed')
        outputs[name] = changed
    return outputs, dict(
        layout='sheng-cn-1tb-lun0-full', disk='sda', disk_lbas=lbas, logical_block_bytes=cn.BLOCK,
        disk_guid=str(uuid.UUID(bytes_le=header[56:72])), entry_count=count,
        macosroot=dict(index=cn.ROOT_INDEX + 1, first_lba=root_first, last_lba=root_last),
        full_index=FULL_INDEX + 1, full_name=FULL_NAME, full_unique_guid=str(full_guid),
        full_first_lba=first, full_last_lba=last, full_bytes=size_bytes,
        unallocated=dict(first_lba=last + 1, last_lba=before['last_usable'],
                         bytes=(before['last_usable'] - last) * cn.BLOCK),
        other_partitions_changed=False, image_written=False, applied=False)


def validate_prepared_full(directory, record=None):
    directory = Path(directory)
    record = record or json.loads((directory / 'plan.json').read_text())
    before = {name: (directory / f'before-{name}.bin').read_bytes() for name in ('primary', 'secondary')}
    for name, data in before.items():
        if len(data) != cn.WINDOW or cn.sha(data) != record.get('before', {}).get(name):
            raise ValueError('prepared before-GPT identity differs: ' + name)
    expected, recomputed = layout_full(before['primary'], before['secondary'], cn.LUN0_LBAS,
                                       record.get('full_bytes'), uuid.UUID(record['full_unique_guid']))
    for key, value in recomputed.items():
        if json.dumps(record.get(key), sort_keys=True) != json.dumps(value, sort_keys=True):
            raise ValueError('prepared plan differs from the recomputed layout: ' + key)
    for name, data in expected.items():
        if (directory / f'after-{name}.bin').read_bytes() != data:
            raise ValueError('prepared after-GPT differs from the recomputed layout: ' + name)
    return dict(validated=True, device_writes=False, full=dict(
        first_lba=recomputed['full_first_lba'], last_lba=recomputed['full_last_lba'],
        bytes=recomputed['full_bytes'], unique_guid=recomputed['full_unique_guid']))


def prepare_full(device, directory, size_bytes, step4=STEP4_LAYOUT):
    """Read-only: the live GPT must equal the verified Step-4 layout before a plan is saved."""
    primary = device.read(0, cn.ALIGN)
    secondary = device.read(device.lbas - cn.ALIGN, cn.ALIGN)
    for name, data in (('primary', primary), ('secondary', secondary)):
        if data != (Path(step4) / f'verified-{name}.bin').read_bytes():
            raise ValueError('current LUN0 GPT differs from the verified Step-4 layout: ' + name)
    outputs, record = layout_full(primary, secondary, device.lbas, size_bytes)
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    for name, data in (('before-primary.bin', primary), ('before-secondary.bin', secondary),
                       ('after-primary.bin', outputs['primary']), ('after-secondary.bin', outputs['secondary'])):
        (directory / name).write_bytes(data)
    record.update(serial=device.serial, step4_layout=str(step4), prepared_at=datetime.now(timezone.utc).isoformat(),
                  before=dict(primary=cn.sha(primary), secondary=cn.sha(secondary)))
    (directory / 'plan.json').write_text(json.dumps(record, indent=2) + '\n')
    return record


def apply_full(device, directory, preserved=PRESERVED_FULL):
    record = json.loads((directory / 'plan.json').read_text())
    if record['applied'] or record['serial'] != device.serial or record['disk_lbas'] != device.lbas:
        raise ValueError('stale or already applied plan')
    device.require_recovery()
    validate_prepared_full(directory, record)
    for name, start in (('primary', 0), ('secondary', device.lbas - cn.ALIGN)):
        raw = device.read(start, cn.ALIGN)
        if cn.sha(raw) != record['before'][name] or raw != (directory / f'before-{name}.bin').read_bytes():
            raise ValueError('LUN0 changed after preparation')
        parse_gpt((directory / f'after-{name}.bin').read_bytes(), cn.BLOCK, device.lbas, start)
    if Path(preserved).exists():
        raise ValueError('a preserved full layout already exists; inspect it before applying again')
    journal = directory / 'apply.jsonl'
    if journal.exists():
        raise ValueError('previous write attempt exists; inspect it before retrying')
    with journal.open('x') as entries:
        def step(kind, path, at, count):
            entries.write(json.dumps(dict(operation='starting', kind=kind, lba=at, count=count)) + '\n')
            entries.flush()
            device.write(path, at, count)
            entries.write(json.dumps(dict(operation='verified', kind=kind)) + '\n')
            entries.flush()
        for name, window in (('secondary', device.lbas - cn.ALIGN), ('primary', 0)):
            data = (directory / f'after-{name}.bin').read_bytes()
            _, header, array, start, _, _ = geometry(data, device.lbas, window)
            blocks = (len(array) + cn.BLOCK - 1) // cn.BLOCK
            path = directory / f'write-{name}-array.bin'
            path.write_bytes(data[(start - window) * cn.BLOCK:(start - window + blocks) * cn.BLOCK])
            step(f'{name}-array', path, start, blocks)
            path = directory / f'write-{name}-header.bin'
            path.write_bytes(header)
            step(f'{name}-header', path, 1 if name == 'primary' else device.lbas - 1, 1)
    Path(preserved).mkdir(parents=True, mode=0o700)
    for name, start in (('primary', 0), ('secondary', device.lbas - cn.ALIGN)):
        data = device.read(start, cn.ALIGN)
        if data != (directory / f'after-{name}.bin').read_bytes():
            raise ValueError('complete GPT readback differs')
        (Path(preserved) / f'verified-{name}.bin').write_bytes(data)
    record.update(applied=True, gpt_readback_verified=True, reboot_required_for_partition_node=True,
                  preserved_layout=str(preserved))
    (directory / 'plan.json').write_text(json.dumps(record, indent=2) + '\n')
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial', required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--gib', type=int, help='macossystem size in GiB (user decision)')
    parser.add_argument('--apply', action='store_true', help='write the prepared GPT (needs explicit user approval)')
    args = parser.parse_args()
    if not args.output.resolve().is_relative_to((cn.ROOT / 'private').resolve()):
        parser.error('--output must stay in local private/')
    device = cn.Device(args.serial)
    if args.apply:
        result = apply_full(device, args.output)
    elif args.gib:
        result = prepare_full(device, args.output, args.gib << 30)
    else:
        parser.error('planning requires --gib')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
