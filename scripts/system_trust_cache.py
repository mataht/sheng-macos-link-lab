#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Rules of the DarwinBoot trust cache bundle: one module of version-2 entries with strictly ascending CDHashes.

The full-system package loads the original trust caches of the delivered system unchanged: DarwinBoot copies
`kernel.macho.trustcache` into the kernel window and fills /chosen/memory-map/TrustCache, and XNU loads module 0 as the
static trust cache. The bundle is made locally, holds no entry of our own and never leaves the local machine; this
module only checks it.
"""
import struct

ENTRY = 24
HEADER = 24


def module_entries(module: bytes) -> tuple[bytes, list[bytes]]:
    if len(module) < HEADER or struct.unpack_from('<I', module)[0] != 2:
        raise ValueError('not a version-2 trust cache module')
    count = struct.unpack_from('<I', module, 20)[0]
    if len(module) != HEADER + count * ENTRY:
        raise ValueError('trust cache module length does not match its entry count')
    entries = [module[HEADER + i * ENTRY:HEADER + (i + 1) * ENTRY] for i in range(count)]
    if any(not 1 <= entry[20] <= 4 for entry in entries):
        raise ValueError('unexpected hash type in an original entry')
    return module[4:20], entries


def bundle(module: bytes) -> bytes:
    """DarwinBoot bundle: a count, one offset per module (from the bundle start), then the modules."""
    return struct.pack('<II', 1, 8) + module


def check_bundle(data: bytes) -> None:
    """The DarwinBoot ValidTrustCaches rules for a one-module bundle."""
    count, offset = struct.unpack_from('<II', data)
    if count != 1 or offset != 8:
        raise ValueError('expected a one-module bundle')
    _, entries = module_entries(data[8:])
    if any(entries[i - 1][:20] >= entries[i][:20] for i in range(1, len(entries))):
        raise ValueError('CDHashes are not strictly ascending')
