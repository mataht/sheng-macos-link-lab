#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Describe the macossystem partition contents for a delivered full-install disk (read-only on the image).

The partition holds a SHNGROOT header block followed by one window "OS" at 1 MiB that is the delivered raw
GPT disk byte for byte (ISC, main container and any Recovery container stay inside it). Nothing is copied:
this writes only the 4096-byte header, a per-chunk SHA-256 map used by the installer and the candidate
full-system root contract for config/macos26-full-root.json.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from full_system_qemu import describe, qualify_inspection  # noqa: E402

MIB = 1 << 20
WINDOW_OFFSET = MIB
CHUNK = 8 * MIB
ZERO = bytes(CHUNK)
ZERO_CHUNK_SHA256 = hashlib.sha256(ZERO).hexdigest()
OVERLAY = dict(boot_argument='native-ufs-root-overlay-mib', default_off=True, minimum_mib=256, maximum_mib=8192,
               persistent=False, disk_writes=False)


def manifest(window_bytes, window_sha256):
    header = bytearray(4096)
    header[:8] = b'SHNGROOT'
    total = (WINDOW_OFFSET + window_bytes + MIB - 1) // MIB * MIB
    struct.pack_into('<IIQ', header, 8, 1, 1, total)
    struct.pack_into('<32sQQII32s40s', header, 24, b'OS', WINDOW_OFFSET, window_bytes, 512, 1,
                     bytes.fromhex(window_sha256), bytes(40))
    header[4064:] = hashlib.sha256(header[:4064]).digest()
    return bytes(header), total


def chunk_map(image, window_bytes):
    """Stream the image once: the window digest and one SHA-256 per 8 MiB chunk (all-zero chunks flagged)."""
    digest, chunks = hashlib.sha256(), []
    with Path(image).open('rb') as stream:
        offset = 0
        while offset < window_bytes:
            data = stream.read(min(CHUNK, window_bytes - offset))
            if not data:
                raise ValueError('image ended before its recorded size')
            digest.update(data)
            zero = data == (ZERO if len(data) == CHUNK else bytes(len(data)))
            chunks.append(dict(offset=offset, bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), zero=zero))
            offset += len(data)
        if stream.read(1):
            raise ValueError('image grew beyond its recorded size')
    return digest.hexdigest(), chunks


def prepare(image, inspection, output):
    identity = qualify_inspection(Path(image), Path(inspection))
    window_bytes = identity['bytes']
    if window_bytes % 4096:
        raise ValueError('the delivered disk must be a whole number of 4096-byte blocks')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    window_sha256, chunks = chunk_map(image, window_bytes)
    header, total = manifest(window_bytes, window_sha256)
    after = Path(image).stat()
    if (after.st_size, after.st_mtime_ns) != (identity['bytes'], identity['mtime_ns']):
        raise ValueError('image changed while it was being described')
    (output / 'manifest.bin').write_bytes(header)
    (output / 'chunks.json').write_text(json.dumps(dict(chunk_bytes=CHUNK, window_offset=WINDOW_OFFSET,
                                                        zero_chunk_sha256=ZERO_CHUNK_SHA256, chunks=chunks)) + '\n')
    contract = dict(build='25F71', variant='full-system', window='OS', bytes=window_bytes, sha256=window_sha256,
                    volume_uuid=str(identity['system_volume']).upper(),
                    volume_group=str(identity['volume_group']).upper(),
                    root_snapshot=identity['root_snapshot'], partition='macossystem', overlay=OVERLAY)
    (output / 'macos26-full-root.json').write_text(json.dumps(contract, indent=1) + '\n')
    record = dict(schema='sheng-full-root-package-v1', image=describe(identity), partition='macossystem',
                  partition_bytes_minimum=total, manifest_sha256=hashlib.sha256(header).hexdigest(),
                  window=dict(name='OS', offset=WINDOW_OFFSET, bytes=window_bytes, sha256=window_sha256),
                  chunk_count=len(chunks), zero_chunks=sum(item['zero'] for item in chunks),
                  chunks_sha256=hashlib.sha256((output / 'chunks.json').read_bytes()).hexdigest(),
                  contract_sha256=hashlib.sha256((output / 'macos26-full-root.json').read_bytes()).hexdigest(),
                  image_modified=False, partition_writes_performed=False, hardware_qualified=False)
    (output / 'build.json').write_text(json.dumps(record, indent=2) + '\n')
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True, type=Path)
    parser.add_argument('--inspection', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(prepare(args.image, args.inspection, args.output), indent=2))


if __name__ == '__main__':
    main()
