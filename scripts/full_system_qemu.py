#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Admission of a delivered full-install image for QEMU observation (host side only).

The delivered raw GPT disk is never written: QEMU attaches it as the backing file
of a new fixture-owned qcow2 overlay, and the guest sees it as the explicitly
described scratch disk. The image identity comes from inspect_macos26_full_image.py.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import uuid

BUILD = '25F71'
# BuildManifest SHA-384 of the decrypted 25F71 OS image (Customer Erase Install, j313ap).
OS_IMAGE_SHA384 = ('7353f467f671fd2f7a55b964aa957f05d92f5b51d1b8bcb6e92f21e2aa9a1e2e'
                   'ef533ca9ecf15d035e04ff44455ca6f5')
OS_IMAGE_NAME = '094-56453-087.dmg'
BLANK_BYTES = 1 << 20
LAUNCHD_SCHEMA = 'sheng-full-launchd-sample-v1'
NIL = str(uuid.UUID(int=0)).upper()


def qualify_inspection(image: Path, report_path: Path) -> dict:
    """Return the root identity of exactly this image, or raise if the report does not describe it."""
    image = image.resolve(strict=True)
    report_bytes = report_path.read_bytes()
    report = json.loads(report_bytes)
    status = image.stat()
    if (report.get('schema') != 'sheng-full-image-inspection-v1' or report.get('read_only') is not True
            or report.get('layout') != 'gpt' or Path(report.get('image', '')).resolve() != image
            or report.get('bytes') != status.st_size or report.get('mtime_ns') != status.st_mtime_ns):
        raise ValueError('inspection report does not describe this unchanged image')
    assessment = report.get('assessment', {})
    groups = assessment.get('system_data_groups', [])
    if assessment.get('single_system_data_group') is not True or len(groups) != 1:
        raise ValueError('requires exactly one System/Data volume group')
    group = groups[0]
    for key in ('volume_group', 'system_volume', 'data_volume'):
        value = group.get(key, '')
        if uuid.UUID(value).int == 0:
            raise ValueError(f'nil {key}')
    if group.get('same_container') is not True:
        raise ValueError('System and Data must share one container')
    if group.get('data_encrypted') is not False:
        raise ValueError('the Data volume is encrypted; this platform has no SEP to unlock it')
    # APFS keeps SEALED_VOLUME and the installer's integrity metadata on the root snapshot (seen on the 25F71 image,
    # 2026-11-08: both snapshots flagged sealed with the same integrity_meta_oid), so the seal flag cannot tell the two
    # apart. Identify the root by name and order instead: one installer snapshot, and a newer snapshot that APFS will
    # mount (root_to_xid). The seal is never verified on these paths: the development root policy (no root hash) is
    # required.
    system = uuid.UUID(group['system_volume'])
    volumes = [v for c in report.get('containers', []) for v in c.get('volumes', [])
               if v.get('uuid') and uuid.UUID(v['uuid']) == system]
    snapshots = {s.get('name'): s for s in (volumes[0].get('snapshots', []) if len(volumes) == 1 else [])}
    root = snapshots.get(group.get('root_snapshot'))
    installer = [s for name, s in snapshots.items() if str(name).startswith('com.apple.os.update-')]
    if (not root or root.get('xid') != group.get('root_to_xid')
            or len(installer) != 1 or not installer[0].get('xid', 0) < root['xid']):
        raise ValueError('the root snapshot must be the one made after the installer snapshot')
    return dict(image=str(image), bytes=status.st_size, mtime_ns=status.st_mtime_ns,
                inspection=str(report_path.resolve()), inspection_sha256=hashlib.sha256(report_bytes).hexdigest(),
                volume_group=uuid.UUID(group['volume_group']), system_volume=system,
                data_volume=uuid.UUID(group['data_volume']), root_snapshot=group['root_snapshot'],
                installer_snapshot=installer[0]['name'],
                root_seal='inherited-broken' if root.get('sealed') else 'unsealed',
                sealed_snapshots=list(group.get('sealed_snapshots', [])))


def describe(identity: dict) -> dict:
    """JSON-safe copy of qualify_inspection's result for run records."""
    return {key: str(value).upper() if isinstance(value, uuid.UUID) else value for key, value in identity.items()}


def create_blank_disk(path: Path) -> dict:
    """Fixture-owned empty first disk; the full image is attached as the writable second disk."""
    with path.open('xb') as stream:
        stream.truncate(BLANK_BYTES)
    return dict(path=str(path), bytes=BLANK_BYTES,
                sha256=hashlib.sha256(bytes(BLANK_BYTES)).hexdigest(), contains_filesystem=False)


SCRATCH_MMIO = '0xa000000'  # QEMU virt virtio-mmio-bus.0


def qemu_disk_arguments(identity: dict, blank: Path, fdt: Path, memory_mib: int, smp: int) -> dict:
    """QEMU, device-tree and gdb-stop arguments for the blank first disk plus the overlaid full image."""
    image = Path(identity['image'])

    def read_only(node, filename):
        return json.dumps(dict(driver='raw', **{'node-name': node, 'read-only': True}, file=dict(
            driver='file', filename=str(filename), **{'read-only': True, 'locking': 'off'})))

    fdt_command = ['qemu-system-aarch64', '-M', f'virt,virtualization=off,secure=off,gic-version=3,dumpdtb={fdt}',
                   '-cpu', 'cortex-a710', '-m', str(memory_mib), '-smp', str(smp), '-nic', 'none',
                   '-display', 'none', '-global', 'virtio-mmio.force-legacy=false',
                   '-blockdev', read_only('root-disk', blank), '-device', 'virtio-blk-device,drive=root-disk',
                   '-blockdev', read_only('isc-disk', image),
                   '-device', 'virtio-blk-device,drive=isc-disk,bus=virtio-mmio-bus.0']
    return dict(
        fdt_command=fdt_command,
        dt_arguments=['--virtio-mmio-fdt', str(fdt), '--qemu-scratch-mmio', SCRATCH_MMIO,
                      '--root-match-volume-uuid', str(identity['system_volume']).upper()],
        gdb_stop_arguments=['--virtio-block-image', str(blank), '--virtio-extra-block-image', str(image),
                            '--virtio-extra-block-overlay'])


def arm64e_slice(data: bytes) -> tuple[int, int]:
    """(offset, size) of the arm64e image: the whole file when thin, the fat-archive slice otherwise."""
    magic = struct.unpack_from('>I', data)[0] if len(data) >= 8 else 0
    if magic not in (0xcafebabe, 0xcafebabf):
        return 0, len(data)
    count = struct.unpack_from('>I', data, 4)[0]
    stride = 20 if magic == 0xcafebabe else 32
    if not 0 < count <= 16 or 8 + count * stride > len(data):
        raise ValueError('invalid universal header')
    found = []
    for index in range(count):
        at = 8 + index * stride
        cpu, subtype = struct.unpack_from('>iI', data, at)
        offset, size = (struct.unpack_from('>II', data, at + 8) if magic == 0xcafebabe
                        else struct.unpack_from('>QQ', data, at + 8))
        if cpu == 0x0100000c and subtype & 0xff == 2:
            if offset + size > len(data):
                raise ValueError('arm64e slice outside the universal file')
            found.append((offset, size))
    if len(found) != 1:
        raise ValueError('requires exactly one arm64e slice')
    return found[0]


def load_launchd_sample(path: Path) -> tuple[bytes, dict]:
    """Return the verified arm64e launchd image (LC_MAIN offsets are relative to it) and its provenance."""
    data = path.read_bytes()
    provenance = json.loads(Path(str(path) + '.json').read_text())
    offset, size = arm64e_slice(data)
    image = data[offset:offset + size]
    if (provenance.get('schema') != LAUNCHD_SCHEMA or provenance.get('build') != BUILD
            or provenance.get('source_image') != OS_IMAGE_NAME
            or provenance.get('source_image_sha384') != OS_IMAGE_SHA384
            or provenance.get('member') != 'sbin/launchd'
            or provenance.get('bytes') != len(data)
            or provenance.get('sha256') != hashlib.sha256(data).hexdigest()
            or provenance.get('arm64e_slice') != dict(offset=offset, bytes=size,
                                                       sha256=hashlib.sha256(image).hexdigest())):
        raise ValueError('launchd sample is not the verified 25F71 OS image member')
    return image, provenance


def extract_launchd(os_image: Path, seven_zip: Path, output: Path) -> dict:
    """Extract sbin/launchd from the decrypted, manifest-verified 25F71 OS image (read-only)."""
    with os_image.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha384').hexdigest() != OS_IMAGE_SHA384:
            raise ValueError('OS image differs from the 25F71 BuildManifest digest')
    output.mkdir(parents=True, exist_ok=False)
    staging = output / 'extract'
    subprocess.run([str(seven_zip), 'e', f'-o{staging}', '-y', str(os_image), 'sbin/launchd'],
                   check=True, capture_output=True, timeout=600)
    data = (staging / 'launchd').read_bytes()
    offset, size = arm64e_slice(data)
    sample = output / 'launchd'
    with sample.open('xb') as stream:
        stream.write(data)
    (staging / 'launchd').unlink()
    staging.rmdir()
    provenance = dict(schema=LAUNCHD_SCHEMA, build=BUILD, source_image=OS_IMAGE_NAME,
                      source_image_sha384=OS_IMAGE_SHA384, member='sbin/launchd', bytes=len(data),
                      sha256=hashlib.sha256(data).hexdigest(),
                      arm64e_slice=dict(offset=offset, bytes=size,
                                        sha256=hashlib.sha256(data[offset:offset + size]).hexdigest()),
                      extractor=str(seven_zip),
                      note='identical to the installed launchd only if the install came from this IPSW; '
                           'the run verifies the actual EL0 entry bytes')
    Path(str(sample) + '.json').write_text(json.dumps(provenance, indent=2) + '\n')
    return provenance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    check = sub.add_parser('check', help='qualify an image against its inspection report')
    check.add_argument('image', type=Path)
    check.add_argument('inspection', type=Path)
    extract = sub.add_parser('extract-launchd', help='extract the paired launchd sample')
    extract.add_argument('--os-image', type=Path, required=True)
    extract.add_argument('--seven-zip', type=Path, required=True)
    extract.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'check':
        print(json.dumps(describe(qualify_inspection(args.image, args.inspection)), indent=2))
    else:
        print(json.dumps(extract_launchd(args.os_image, args.seven_zip, args.output), indent=2))


if __name__ == '__main__':
    main()
