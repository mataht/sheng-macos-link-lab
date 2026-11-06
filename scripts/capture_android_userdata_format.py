#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Record, read-only, that Android rebuilt userdata inside the resized partition (installer --android-format)."""
import argparse
import json
from pathlib import Path
import re
import subprocess

BLOCK = 4096


def parse(serial: str, plan: dict, outputs: dict) -> dict:
    """Build the evidence install_macos26_ufs_root.qualify_android_format checks from plain shell outputs."""
    mounts = [line.split() for line in outputs['mounts'].splitlines()]
    data = [m for m in mounts if len(m) >= 3 and m[1] == '/data']
    if len(data) != 1:
        raise ValueError('requires exactly one /data mount')
    source, filesystem = data[0][0], data[0][2]
    slaves = outputs['slaves'].split()
    # Metadata encryption puts /data on a dm device whose only slave must be the resized userdata partition.
    physical = slaves[0] if re.fullmatch(r'/dev/block/dm-\d+', source) and len(slaves) == 1 else source.rsplit('/', 1)[-1]
    userdata_sectors = int(outputs['userdata_size'])
    root_start, root_sectors = int(outputs['root_start']), int(outputs['root_size'])
    root_name = re.search(r'^PARTNAME=(\S+)$', outputs['root_uevent'], re.M)
    # A non-root shell cannot read the GPT GUID; it is taken from the applied plan only after the kernel's own
    # macosroot node matches that plan's name and extent.
    if (not root_name or root_name.group(1) != plan['root_name']
            or root_start * 512 != plan['root_first_lba'] * BLOCK
            or root_sectors * 512 != (plan['root_last_lba'] - plan['root_first_lba'] + 1) * BLOCK):
        raise ValueError('kernel macosroot node differs from the applied plan')
    block_size, blocks = (int(value) for value in outputs['statfs'].split())
    return dict(schema='sheng-android-userdata-format-v1', serial=serial, read_only=True,
                user_setup_complete=outputs['setup'].strip(), filesystem=filesystem,
                mount_source=source, physical_partition=physical, physical_disk=re.sub(r'\d+$', '', physical),
                userdata_partition_bytes=userdata_sectors * 512, filesystem_bytes=block_size * blocks,
                root_partition_guid=plan['root_unique_guid'],
                root_partition_guid_source='applied plan; kernel macosroot name and extent matched',
                device_writes=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial', required=True)
    parser.add_argument('--partition-plan', required=True, type=Path, help='applied plan.json directory')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('choose a new output file')
    plan = json.loads((args.partition_plan / 'plan.json').read_text())
    if plan.get('applied') is not True or plan.get('serial') != args.serial:
        parser.error('requires the applied plan for this device')
    adb = ['adb', '-s', args.serial]
    state = subprocess.check_output(adb + ['get-state'], text=True, timeout=10).strip()
    if state != 'device':
        parser.error('requires booted Android (setup wizard), not Recovery')

    def shell(command):
        return subprocess.check_output(adb + ['shell', command], text=True, timeout=30).strip()

    if shell('getprop ro.product.device') != 'sheng':
        parser.error('requires sheng')
    data_source = next((line.split()[0] for line in shell('cat /proc/mounts').splitlines()
                        if len(line.split()) >= 3 and line.split()[1] == '/data'), '')
    dm = data_source.rsplit('/', 1)[-1]
    index = plan['root_index']
    outputs = dict(
        mounts=shell('cat /proc/mounts'),
        slaves=shell(f'ls /sys/block/{dm}/slaves') if dm.startswith('dm-') else '',
        userdata_size=shell('cat /sys/class/block/sda29/size'),
        root_start=shell(f'cat /sys/class/block/sda{index}/start'),
        root_size=shell(f'cat /sys/class/block/sda{index}/size'),
        root_uevent=shell(f'cat /sys/class/block/sda{index}/uevent'),
        statfs=shell('stat -f -c "%S %b" /data'),
        setup=shell('settings get secure user_setup_complete'))
    evidence = parse(args.serial, plan, outputs)
    args.output.write_text(json.dumps(dict(evidence, raw=outputs), indent=2) + '\n')
    print(json.dumps(evidence, indent=2))


if __name__ == '__main__':
    main()
