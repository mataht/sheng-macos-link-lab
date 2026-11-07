#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Write only the prepared macosroot partition from root sheng Recovery."""
import argparse
from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import sys
import uuid

from create_log_partition import geometry
from capture_recovery_userdata_format import qualify_recovery_format
from native_log_file import LAYOUT
if LAYOUT['name'] == 'sheng-cn-1tb-lun0':
    # CN 1 TB unit: the plan comes from repartition_sheng_cn_1tb.py, whose Device checks this LUN0 and Recovery.
    from repartition_sheng_cn_1tb import BLOCK, WINDOW, Device
else:
    from repartition_macos26_root import BLOCK, WINDOW, Device


PARTITION = '/dev/block/by-name/macosroot'


def require_safe_transport(staged_image):
    if staged_image is None and sys.platform == 'win32':
        raise ValueError('Windows ADB stdin is not qualified; requires --staged-image in verified /tmp RAM')
    if staged_image is not None and not re.fullmatch(r'/tmp/sheng-macosroot-[A-Za-z0-9_-]+/macosroot\.img', staged_image):
        raise ValueError('staged image must use /tmp/sheng-macosroot-<safe-id>/macosroot.img')


def qualify_staged_image(device, staged_image, image_bytes, image_sha256):
    mounts = [line.split() for line in device.shell('cat /proc/mounts').splitlines()]
    tmp = [mount for mount in mounts if len(mount) >= 3 and mount[1] == '/tmp']
    directory = staged_image.rsplit('/', 1)[0]
    if (len(tmp) != 1 or tmp[0][2] != 'tmpfs'
            or any(len(mount) >= 2 and (mount[1] == directory or mount[1].startswith(directory + '/'))
                   for mount in mounts)):
        raise ValueError('staged image requires /tmp tmpfs without an overlaid staging directory or file')
    if device.shell('readlink -f ' + staged_image) != staged_image:
        raise ValueError('staged image realpath differs from its approved RAM path')
    device.shell('test -f ' + staged_image)
    if device.shell('stat -c %s ' + staged_image) != str(image_bytes):
        raise ValueError('staged image size differs from the local image')
    if device.shell('sha256sum ' + staged_image, timeout=120).split() != [image_sha256, staged_image]:
        raise ValueError('staged image SHA-256 differs from the local image')
    return dict(path=staged_image, bytes=image_bytes, sha256=image_sha256, filesystem='tmpfs')


def qualify_android_format(evidence, plan, serial):
    if (evidence.get('serial') != serial or evidence.get('read_only') is not True
            or evidence.get('root_partition_guid') != plan['root_unique_guid']
            or evidence.get('userdata_partition_bytes') != plan['userdata_bytes']
            or evidence.get('user_setup_complete') != '0'
            or evidence.get('filesystem') != 'f2fs'
            or evidence.get('physical_partition') != 'sda29' or evidence.get('physical_disk') != 'sda'
            or not (16 << 30) <= evidence.get('filesystem_bytes', 0) <= plan['userdata_bytes']):
        raise ValueError('requires the freshly rebuilt Android filesystem within resized userdata')


def select_discard_command(device):
    """Probe help without a device argument; never try another tool after an actual discard."""
    probes = []
    for tool in ('blkdiscard', 'toybox blkdiscard'):
        command = tool + ' --help'
        try:
            device.shell(command, timeout=10)
        except subprocess.CalledProcessError as error:
            probes.append(dict(command=command, returncode=error.returncode))
        else:
            probes.append(dict(command=command, returncode=0))
            return tool + ' ' + PARTITION, probes
    raise ValueError('no usable blkdiscard tool; help probes failed: ' + json.dumps(probes))


def install(device, plan_dir, root_dir, output, android_format=None, recovery_format=None, staged_image=None):
    require_safe_transport(staged_image)
    if android_format and recovery_format:
        raise ValueError('Android and stock Recovery filesystem evidence are mutually exclusive')
    plan_bytes = (plan_dir / 'plan.json').read_bytes()
    plan = json.loads(plan_bytes)
    build = json.loads((root_dir / 'build.json').read_text())
    image = root_dir / 'macosroot.img'
    manifest = (root_dir / 'manifest.bin').read_bytes()
    if (plan.get('applied') is not True or plan.get('gpt_readback_verified') is not True
            or plan.get('serial') != device.serial or plan.get('root_name') != 'macosroot'
            or build.get('partition') != 'macosroot' or build.get('build') != '25F71'
            or build.get('root_lun') != 0 or build.get('android_userdata_used') is not False
            or build['image_bytes'] != image.stat().st_size or image.stat().st_size % WINDOW
            or image.stat().st_size > plan['root_bytes'] or len(manifest) != BLOCK
            or manifest[:8] != b'SHNGROOT'
            or hashlib.sha256(manifest[:4064]).digest() != manifest[4064:]
            or hashlib.sha256(manifest).hexdigest() != build['manifest_sha256']):
        raise ValueError('requires the applied dedicated partition and paired root image')
    device.require_recovery()
    for kind, start in (('primary', 0), ('secondary', device.lbas - WINDOW // BLOCK)):
        current = device.read(start, WINDOW // BLOCK)
        expected = (plan_dir / f'after-{kind}.bin').read_bytes()
        # Primary and backup GPT windows must still match byte for byte; reject unrelated partition changes.
        if current != expected:
            raise ValueError('LUN0 layout changed since the applied partition plan')
        _, _, entries, _, _, stride = geometry(current, device.lbas, start)
        entry = entries[(plan['root_index'] - 1) * stride:plan['root_index'] * stride]
        if (entry[16:32] != uuid.UUID(plan['root_unique_guid']).bytes_le
                or struct.unpack_from('<QQ', entry, 32) != (plan['root_first_lba'], plan['root_last_lba'])):
            raise ValueError('macosroot identity or extent differs from the plan')
    if (device.shell('readlink -f ' + PARTITION) != '/dev/block/sda' + str(plan['root_index'])
            or int(device.shell('blockdev --getsize64 ' + PARTITION)) != plan['root_bytes']
            or device.shell(f'ls -A /sys/class/block/sda{plan["root_index"]}/holders')):
        raise ValueError('kernel macosroot node differs from the verified GPT')
    log, format_bytes = None, None
    if android_format or recovery_format:
        format_bytes = (android_format or recovery_format).read_bytes()
        if android_format:
            qualify_android_format(json.loads(format_bytes), plan, device.serial)
        else:
            qualify_recovery_format(json.loads(format_bytes), plan_bytes, device.serial)
        if int(device.shell('blockdev --getsize64 /dev/block/by-name/userdata')) != plan['userdata_bytes']:
            raise ValueError('current userdata node differs from the observed filesystem boundary')
    else:
        log = device.shell('cat /tmp/recovery.log')
        size_marker = re.escape(
            f'wipe metadata encrypted /dev/block/bootdevice/by-name/userdata with size {plan["userdata_bytes"]}')
        if 'Data wipe complete.' not in log or not re.search(size_marker + r'(?=\s|$)', log):
            raise ValueError('requires completed Recovery wipe or observed rebuilt Android filesystem')
    with image.open('rb') as stream:
        if stream.read(BLOCK) != manifest:
            raise ValueError('root image manifest differs from the prepared manifest')
        stream.seek(0)
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    staged = qualify_staged_image(device, staged_image, image.stat().st_size, digest) if staged_image is not None else None
    write_command = ('dd ' + ('if=' + staged_image + ' ' if staged is not None else '')
                     + 'of=' + PARTITION + ' bs=1048576 conv=notrunc,fsync')
    discard_command, discard_probes = select_discard_command(device)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    if android_format or recovery_format:
        evidence_file = 'android-format.json' if android_format else 'recovery-format.json'
        (output / evidence_file).write_bytes(format_bytes)
        reset_evidence = dict(kind='android-filesystem' if android_format else 'stock-recovery-filesystem',
                              source=str(android_format or recovery_format),
                              capture='local-file-bytes', recovery_wipe_observed=False)
    else:
        evidence_file = 'recovery-wipe.log'
        (output / evidence_file).write_text(log, encoding='utf-8')
        reset_evidence = dict(kind='recovery-log', source='/tmp/recovery.log',
                              capture='decoded-adb-shell-output', recovery_wipe_observed=True)
    reset_evidence.update(file=evidence_file,
                          artifact_sha256=hashlib.sha256((output / evidence_file).read_bytes()).hexdigest())
    record = dict(serial=device.serial, started_at=datetime.now(timezone.utc).isoformat(),
                  destination=PARTITION, destination_guid=plan['root_unique_guid'],
                  bytes=image.stat().st_size, sha256=digest, status='prepared',
                  userdata_discard_authorized=True, gpt_writes=False, image_write_attempts=0,
                  image_write_command=write_command, image_write_transport='staged-ram-file' if staged else 'adb-stdin',
                  staged_image=staged,
                  discard_command=discard_command, discard_tool_probes=discard_probes, discard_attempts=0,
                  userdata_reset_evidence=reset_evidence)
    def save():
        (output / 'deployment.json').write_text(json.dumps(record, indent=2) + '\n')
    save()
    record.update(status='discarding-dedicated-root', discard_attempts=1)
    save()
    try:
        device.shell(discard_command)
    except (subprocess.SubprocessError, OSError) as error:
        record.update(status='discard-failed', discard_returncode=getattr(error, 'returncode', None),
                      discard_error=dict(type=type(error).__name__, message=str(error)),
                      failed_at=datetime.now(timezone.utc).isoformat())
        save()
        raise
    record.update(status='writing-root', discard_returncode=0, image_write_attempts=1)
    save()
    try:
        with (nullcontext(subprocess.DEVNULL) if staged else image.open('rb')) as source, (output / 'write.log').open('w') as log_stream:
            subprocess.run(device.adb + ['shell', '-T', write_command], stdin=source, stdout=log_stream,
                stderr=subprocess.STDOUT, check=True, timeout=240)
    except (subprocess.SubprocessError, OSError) as error:
        record.update(status='write-failed', image_write_returncode=getattr(error, 'returncode', None),
                      image_write_error=dict(type=type(error).__name__, message=str(error)),
                      failed_at=datetime.now(timezone.utc).isoformat())
        save()
        raise
    record.update(status='verifying-root', image_write_returncode=0)
    save()
    device.shell('sync; blockdev --flushbufs ' + PARTITION, timeout=120)
    count = image.stat().st_size // WINDOW
    observed = device.shell(f'set -o pipefail; dd if={PARTITION} bs=1048576 count={count} 2>/tmp/sheng-root-readback.log | sha256sum', timeout=120).split()[0]
    (output / 'readback.log').write_text(device.shell('cat /tmp/sheng-root-readback.log') + '\n')
    record['readback_sha256'] = observed
    if observed != digest:
        record['status'] = 'readback-mismatch'
        save()
        raise ValueError('macosroot readback differs; no automatic retry')
    record.update(status='verified', readback_verified=True, completed_at=datetime.now(timezone.utc).isoformat())
    save()
    plan.update(root_image_written=True, root_image_bytes=image.stat().st_size,
                root_image_sha256=digest, root_image_readback_verified=True,
                userdata_contents_discarded=True,
                # Filesystem observations do not directly observe the preceding Recovery wipe event.
                userdata_recovery_wipe_completed=None if android_format or recovery_format else True,
                userdata_reset_evidence_kind=reset_evidence['kind'],
                userdata_filesystem_rebuilt=bool(android_format or recovery_format),
                # Stock Recovery directly mounts F2FS; Android vold/metadata encryption is still unobserved.
                userdata_format_pending_vold=not bool(android_format))
    if recovery_format:
        plan['userdata_android_initialization_observed'] = False
    (plan_dir / 'plan.json').write_text(json.dumps(plan, indent=2) + '\n')
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial', required=True)
    parser.add_argument('--partition-plan', required=True, type=Path)
    parser.add_argument('--root-build', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--staged-image', help='verified RAM image at /tmp/sheng-macosroot-<safe-id>/macosroot.img')
    evidence = parser.add_mutually_exclusive_group()
    evidence.add_argument('--android-format', type=Path,
                          help='read-only Android statfs evidence after Recovery wipe and filesystem rebuild')
    evidence.add_argument('--recovery-format', type=Path,
                          help='qualified saved stock Recovery filesystem observations (not an Android initialization)')
    args = parser.parse_args()
    require_safe_transport(args.staged_image)
    print(json.dumps(install(Device(args.serial), args.partition_plan, args.root_build, args.output,
                             args.android_format, args.recovery_format, args.staged_image), indent=2))


if __name__ == '__main__':
    main()
