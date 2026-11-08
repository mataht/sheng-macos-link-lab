#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only metadata report for a delivered macOS full-install disk image.

Parses only the GPT, APFS container superblocks, container object maps, volume
superblocks and snapshot metadata trees. It never opens the image for writing,
never parses file-system trees and never decompresses file data.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import struct
import sys
import uuid
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prepare_qemu_isc import checksum  # noqa: E402

BLOCK = 4096
APFS_TYPE = uuid.UUID('7c3457ef-0000-11aa-aa11-00306543ecac')
ISC_TYPE = uuid.UUID('69646961-6700-11aa-aa11-00306543ecac')
RECOVERY_TYPE = uuid.UUID('52637672-7900-11aa-aa11-00306543ecac')
CONTAINER_TYPES = {APFS_TYPE: 'apfs', ISC_TYPE: 'isc', RECOVERY_TYPE: 'recovery'}
ROLES = {0: 'None', 0x1: 'System', 0x2: 'User', 0x4: 'Recovery', 0x8: 'VM', 0x10: 'Preboot',
         0x20: 'Installer', 0x40: 'Data', 0x80: 'Baseband', 0xc0: 'Update', 0x100: 'xART',
         0x140: 'Hardware', 0x180: 'Backup', 0x240: 'Enterprise', 0x2c0: 'Prelogin'}
INCOMPAT = {0x1: 'CASE_INSENSITIVE', 0x2: 'DATALESS_SNAPS', 0x4: 'ENC_ROLLED',
            0x8: 'NORMALIZATION_INSENSITIVE', 0x10: 'INCOMPLETE_RESTORE', 0x20: 'SEALED_VOLUME'}
FS_UNENCRYPTED = 0x1
OMAP_VAL_DELETED = 0x1
TYPE_SNAP_METADATA = 1
MAX_NODES = 1 << 16
MAX_VOLUMES = 100


class Window:
    """Bounded read-only view of one byte range of the image file."""

    def __init__(self, stream, base, size):
        if base < 0 or size <= 0:
            raise ValueError('empty or negative window')
        self.stream, self.base, self.size = stream, base, size

    def read(self, offset, length):
        if offset < 0 or length < 0 or offset + length > self.size:
            raise ValueError(f'read outside window: {offset:#x}+{length:#x}')
        self.stream.seek(self.base + offset)
        data = self.stream.read(length)
        if len(data) != length:
            raise ValueError('image ends before the described window')
        return data

    def sub(self, offset, size):
        if offset < 0 or size <= 0 or offset + size > self.size:
            raise ValueError('sub-window outside parent')
        return Window(self.stream, self.base + offset, size)


def parse_gpt(window):
    for sector in (512, 4096):
        if window.size < sector * 2:
            continue
        header = window.read(sector, 92)
        if header[:8] != b'EFI PART':
            continue
        size = struct.unpack_from('<I', header, 12)[0]
        if not 92 <= size <= sector:
            raise ValueError('invalid GPT header size')
        header = bytearray(window.read(sector, size))
        stored = struct.unpack_from('<I', header, 16)[0]
        header[16:20] = bytes(4)
        if zlib.crc32(header) != stored:
            raise ValueError('GPT header CRC mismatch')
        current, backup, first_usable, last_usable = struct.unpack_from('<QQQQ', header, 24)
        entries_lba, count, entry_size, entries_crc = struct.unpack_from('<QIII', header, 72)
        if current != 1 or not 128 <= entry_size <= 1024 or entry_size % 128 or not 0 < count <= 1024:
            raise ValueError('unsupported GPT geometry')
        table = window.read(entries_lba * sector, count * entry_size)
        if zlib.crc32(table) != entries_crc:
            raise ValueError('GPT entry array CRC mismatch')
        partitions = []
        for index in range(count):
            entry = table[index * entry_size:(index + 1) * entry_size]
            kind = uuid.UUID(bytes_le=entry[:16])
            if kind.int == 0:
                continue
            first, last, attributes = struct.unpack_from('<QQQ', entry, 32)
            if not first_usable <= first <= last <= last_usable:
                raise ValueError(f'GPT entry {index} outside the usable range')
            partitions.append(dict(
                index=index, type=str(kind), kind=CONTAINER_TYPES.get(kind, 'other'),
                unique=str(uuid.UUID(bytes_le=entry[16:32])), first_lba=first, last_lba=last,
                offset=first * sector, bytes=(last - first + 1) * sector, attributes=attributes,
                name=entry[56:128].decode('utf-16le', 'replace').split('\0', 1)[0]))
        return dict(sector=sector, disk=str(uuid.UUID(bytes_le=bytes(header[56:72]))),
                    backup_lba=backup, partitions=partitions)
    return None


def valid_object(block, magic=None):
    return (len(block) == BLOCK and any(block[8:]) and block[:8] == checksum(block)
            and (magic is None or block[32:36] == magic))


def object_header(block):
    oid, xid, kind, subtype = struct.unpack_from('<QQII', block, 8)
    return oid, xid, kind & 0xffff, subtype


def btree_records(window, root, *, fixed=None):
    """Yield leaf (key, value) pairs of a B-tree whose child pointers are physical block numbers."""
    pending, seen, key_size, value_size = [root], set(), None, None
    while pending:
        address = pending.pop()
        if address in seen or len(seen) >= MAX_NODES:
            raise ValueError('B-tree cycle or node limit')
        seen.add(address)
        node = window.read(address * BLOCK, BLOCK)
        if not valid_object(node) or object_header(node)[2] not in (2, 3):
            raise ValueError(f'invalid B-tree node at block {address}')
        flags, level, count = struct.unpack_from('<HHI', node, 32)
        table_offset, table_length = struct.unpack_from('<HH', node, 40)
        root_node, leaf, fixed_kv = flags & 1, flags & 2, flags & 4
        if bool(leaf) != (level == 0):
            raise ValueError('inconsistent B-tree leaf flag')
        if root_node and address == root and fixed_kv:
            key_size, value_size = struct.unpack_from('<II', node, BLOCK - 40 + 8)
        if fixed_kv and (fixed is None or (key_size, value_size) != fixed):
            raise ValueError('unexpected fixed-size B-tree record geometry')
        keys = 56 + table_offset + table_length
        values = BLOCK - (40 if root_node else 0)
        stride = 4 if fixed_kv else 8
        if 56 + table_offset + count * stride > keys:
            raise ValueError('B-tree table of contents overflows')
        for index in range(count):
            at = 56 + table_offset + index * stride
            if fixed_kv:
                key_offset, value_offset = struct.unpack_from('<HH', node, at)
                key_length, value_length = fixed[0], (fixed[1] if leaf else 8)
            else:
                key_offset, key_length, value_offset, value_length = struct.unpack_from('<HHHH', node, at)
            key_at, value_at = keys + key_offset, values - value_offset
            if not (keys <= key_at and key_at + key_length <= values and keys <= value_at
                    and value_at + value_length <= values):
                raise ValueError('B-tree record outside its node')
            key, value = node[key_at:key_at + key_length], node[value_at:value_at + value_length]
            if leaf:
                yield key, value
            else:
                pending.append(struct.unpack_from('<Q', value)[0])


def object_map(window, omap_address, newest_xid):
    omap = window.read(omap_address * BLOCK, BLOCK)
    if not valid_object(omap) or object_header(omap)[2] != 0xb:
        raise ValueError('invalid container object map')
    tree = struct.unpack_from('<Q', omap, 48)[0]
    mapping = {}
    for key, value in btree_records(window, tree, fixed=(16, 16)):
        oid, xid = struct.unpack('<QQ', key)
        flags, size, address = struct.unpack('<IIQ', value)
        if xid > newest_xid or (oid in mapping and mapping[oid][0] >= xid):
            continue
        mapping[oid] = (xid, None if flags & OMAP_VAL_DELETED else address)
    return {oid: address for oid, (_, address) in mapping.items() if address is not None}


def nanoseconds(value):
    if not value:
        return None
    return datetime.fromtimestamp(value / 1e9, timezone.utc).isoformat()


def describe_volume(block):
    features, ro_compat, incompat = struct.unpack_from('<QQQ', block, 40)
    flags = struct.unpack_from('<Q', block, 264)[0]
    role = struct.unpack_from('<H', block, 964)[0]
    integrity, fext = struct.unpack_from('<QQ', block, 1024)
    return dict(
        name=block[704:960].split(b'\0', 1)[0].decode('utf-8', 'replace'),
        uuid=str(uuid.UUID(bytes=bytes(block[240:256]))).upper(),
        volume_group=str(uuid.UUID(bytes=bytes(block[1008:1024]))).upper(),
        role=role, role_name=ROLES.get(role, f'{role:#x}'), fs_flags=flags,
        encrypted=not flags & FS_UNENCRYPTED,
        incompatible=[name for bit, name in INCOMPAT.items() if incompat & bit],
        incompatible_raw=incompat, features=features, readonly_compatible=ro_compat,
        sealed=bool(incompat & 0x20), integrity_meta_oid=integrity, fext_tree_oid=fext,
        files=struct.unpack_from('<Q', block, 184)[0], directories=struct.unpack_from('<Q', block, 192)[0],
        symlinks=struct.unpack_from('<Q', block, 200)[0], snapshot_count=struct.unpack_from('<Q', block, 216)[0],
        allocated_blocks=struct.unpack_from('<Q', block, 88)[0],
        root_to_xid=struct.unpack_from('<Q', block, 968)[0],
        snapshot_tree=struct.unpack_from('<Q', block, 152)[0],
        last_xid=object_header(block)[1])


def snapshots(window, volume):
    if not volume['snapshot_tree']:
        return []
    found = []
    for key, value in btree_records(window, volume['snapshot_tree']):
        identity = struct.unpack_from('<Q', key)[0]
        if identity >> 60 != TYPE_SNAP_METADATA:
            continue
        if len(value) < 50:
            raise ValueError('truncated snapshot metadata')
        _, superblock, created, changed, _, _, flags, length = struct.unpack_from('<QQQQQIIH', value)
        if 50 + length > len(value):
            raise ValueError('snapshot name exceeds its record')
        record = dict(xid=identity & ((1 << 60) - 1), flags=flags, created=nanoseconds(created),
                      name=value[50:50 + length].split(b'\0', 1)[0].decode('utf-8', 'replace'),
                      superblock_block=superblock)
        if superblock:
            block = window.read(superblock * BLOCK, BLOCK)
            if valid_object(block, b'APSB'):
                state = describe_volume(block)
                record.update(sealed=state['sealed'], integrity_meta_oid=state['integrity_meta_oid'])
        found.append(record)
    return sorted(found, key=lambda item: item['xid'])


def inspect_container(window):
    first = window.read(0, BLOCK)
    if not valid_object(first, b'NXSB'):
        raise ValueError('no valid APFS container superblock')
    block_size, block_count = struct.unpack_from('<IQ', first, 36)
    if block_size != BLOCK or block_count * BLOCK > window.size:
        raise ValueError('unsupported APFS block size or container beyond its partition')
    descriptor_blocks = struct.unpack_from('<I', first, 104)[0]
    descriptor_base = struct.unpack_from('<Q', first, 112)[0]
    if descriptor_blocks & 0x80000000:
        raise ValueError('non-contiguous checkpoint descriptor areas are not supported')
    newest = first
    for index in range(descriptor_blocks):
        block = window.read((descriptor_base + index) * BLOCK, BLOCK)
        if valid_object(block, b'NXSB') and object_header(block)[1] > object_header(newest)[1]:
            newest = block
    xid = object_header(newest)[1]
    mapping = object_map(window, struct.unpack_from('<Q', newest, 160)[0], xid)
    maximum = struct.unpack_from('<I', newest, 180)[0]
    volumes = []
    for slot, oid in enumerate(struct.unpack_from(f'<{MAX_VOLUMES}Q', newest, 184)[:maximum]):
        if not oid:
            continue
        if oid not in mapping:
            raise ValueError(f'volume object {oid} missing from the container object map')
        block = window.read(mapping[oid] * BLOCK, BLOCK)
        if not valid_object(block, b'APSB'):
            raise ValueError(f'invalid volume superblock for object {oid}')
        volume = describe_volume(block)
        volume.update(slot=slot, oid=oid, snapshots=snapshots(window, volume))
        root = [item for item in volume['snapshots'] if item['xid'] == volume['root_to_xid']]
        volume['root_snapshot'] = root[0]['name'] if root else None
        volumes.append(volume)
    return dict(uuid=str(uuid.UUID(bytes=bytes(newest[72:88]))).upper(), block_count=block_count,
                bytes=block_count * BLOCK, checkpoint_xid=xid, volumes=volumes)


def assess(containers):
    """Facts the full-system boot design depends on; values are reported, not enforced."""
    groups = {}
    for container in containers:
        for volume in container['volumes']:
            if volume['volume_group'] != str(uuid.UUID(int=0)).upper():
                groups.setdefault(volume['volume_group'], []).append((container['uuid'], volume))
    candidates = []
    for group, members in sorted(groups.items()):
        roles = {volume['role_name']: (container, volume) for container, volume in members}
        if 'System' not in roles or 'Data' not in roles:
            continue
        container, system = roles['System']
        data = roles['Data'][1]
        root = [item for item in system['snapshots'] if item['xid'] == system['root_to_xid']]
        candidates.append(dict(
            volume_group=group, container=container, system_volume=system['uuid'],
            system_name=system['name'], data_volume=data['uuid'], data_encrypted=data['encrypted'],
            root_to_xid=system['root_to_xid'], root_snapshot=root[0]['name'] if root else None,
            root_snapshot_sealed=root[0].get('sealed') if root else None,
            live_system_sealed=system['sealed'],
            sealed_snapshots=[item['name'] for item in system['snapshots'] if item.get('sealed')],
            same_container=all(owner == container for owner, _ in members)))
    return dict(system_data_groups=candidates, single_system_data_group=len(candidates) == 1)


def inspect(path, *, hash_partitions=False):
    path = Path(path)
    with path.open('rb') as stream:
        image = Window(stream, 0, path.stat().st_size)
        report = dict(schema='sheng-full-image-inspection-v1', image=str(path.resolve()),
                      bytes=image.size, read_only=True)
        gpt = parse_gpt(image)
        report['gpt'] = gpt
        containers = []
        if gpt is None:
            report['layout'] = 'bare-container'
            item = inspect_container(image)
            item.update(partition=None, offset=0)
            containers.append(item)
        else:
            report['layout'] = 'gpt'
            for partition in gpt['partitions']:
                if partition['offset'] + partition['bytes'] > image.size:
                    raise ValueError(f"partition {partition['index']} extends past the image")
                if hash_partitions:
                    digest = hashlib.sha256()
                    stream.seek(partition['offset'])
                    left = partition['bytes']
                    while left:
                        chunk = stream.read(min(left, 8 << 20))
                        if not chunk:
                            raise ValueError('image ended while hashing a partition')
                        digest.update(chunk)
                        left -= len(chunk)
                    partition['sha256'] = digest.hexdigest()
                if partition['kind'] == 'other':
                    continue
                item = inspect_container(image.sub(partition['offset'], partition['bytes']))
                item.update(partition=partition['index'], partition_kind=partition['kind'],
                            offset=partition['offset'])
                containers.append(item)
        report['containers'] = containers
        report['assessment'] = assess(containers)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('--hash-partitions', action='store_true',
                        help='also SHA-256 every GPT partition (reads the whole image)')
    parser.add_argument('--output', type=Path, help='write the JSON report here (refuses to overwrite)')
    args = parser.parse_args()
    if not args.image.is_file():
        parser.error('requires an existing image file')
    report = inspect(args.image, hash_partitions=args.hash_partitions)
    text = json.dumps(report, indent=2, ensure_ascii=False) + '\n'
    if args.output:
        with args.output.open('x', encoding='utf-8') as stream:
            stream.write(text)
    else:
        sys.stdout.write(text)


if __name__ == '__main__':
    sys.dont_write_bytecode = True
    main()
