// SPDX-License-Identifier: MIT
#pragma once
// Volatile RAM overlay for the full-system root window (development mode only).
//
// The window's UFS contents stay the read-only seed: a page absent from the overlay is read from the
// seed, the first write to a page copies the seed page into RAM, and every later access uses that copy.
// Nothing here ever writes to a disk. Writes beyond residentLimit fail with kIOReturnNoMemory and report
// the bytes actually completed. Contents are lost at shutdown.
//
// A flat page table would need 8 bytes per 4 KiB of window (160 MiB for 80 GiB), so the first level holds
// one pointer per 2 MiB and the page-sized second-level tables are allocated on the first write into their
// range. Reads of seed pages are batched up to kDataMax like the read-only window path.
//
// Requires RootWindowRead.h (UfsWindowResult, UfsWindowGuard, ufsWindowGeometry) and kDataMax.
// Ops: lock/unlock (serializes the window), allocate (one 4096-byte page or nullptr), release(page),
// read(lba, blocks, out) seed reads, load(offset, out, n) from the caller's buffer, copy(offset, data, n)
// into the caller's buffer.
#include <stdint.h>
#include <string.h>

struct UfsTieredDisk {
  uint8_t ***tables;   // first level: chunks entries, owned by the caller
  uint64_t bytes;      // window size
  uint64_t chunks;     // ufsTieredChunks (bytes)
  uint64_t resident;   // overlaid data bytes
  uint64_t tableBytes; // second-level table bytes
};
static constexpr uint64_t kTieredPagesPerTable = 4096 / sizeof (uint8_t *);
static constexpr uint64_t kTieredMaxBytes = 512ULL << 30;

static inline bool ufsTieredGeometry (uint64_t bytes)
{
  return bytes && !(bytes & 4095) && bytes <= kTieredMaxBytes;
}
static inline uint64_t ufsTieredChunks (uint64_t bytes)
{
  return (bytes / 4096 + kTieredPagesPerTable - 1) / kTieredPagesPerTable;
}
static inline const uint8_t *ufsTieredFind (const UfsTieredDisk &disk, uint64_t page)
{
  uint8_t **table = disk.tables[page / kTieredPagesPerTable];
  return table ? table[page % kTieredPagesPerTable] : nullptr;
}

template <class Ops>
static UfsWindowResult ufsTieredOverlayIO (Ops &ops, UfsTieredDisk &disk, uint64_t absolute, bool write,
                                           uint64_t block, uint64_t count, uint64_t residentLimit, uint8_t *bounce)
{
  if (!disk.tables || !bounce || !ufsTieredGeometry (disk.bytes) || disk.chunks != ufsTieredChunks (disk.bytes) ||
      !ufsWindowGeometry (absolute, disk.bytes) || !residentLimit || (residentLimit & 4095) || !count ||
      block >= disk.bytes / 512 || count > disk.bytes / 512 - block)
    return { kIOReturnBadArgument, 0 };
  UfsWindowGuard<Ops> guard (ops);
  const uint64_t offset = block * 512, length = count * 512, seed = absolute / 4096;
  const uint64_t lastPage = (offset + length - 1) / 4096, batch = kDataMax / 4096;
  UfsWindowResult result = { kIOReturnSuccess, 0 };
  while (result.done < length) {
    uint64_t at = offset + result.done, page = at / 4096, skip = at & 4095;
    uint8_t **&table = disk.tables[page / kTieredPagesPerTable];
    uint8_t *data = table ? table[page % kTieredPagesPerTable] : nullptr;
    if (!write && !data) {
      uint64_t blocks = 1;
      while (blocks < batch && page + blocks <= lastPage && !ufsTieredFind (disk, page + blocks)) ++blocks;
      result.status = ops.read (seed + page, static_cast<uint32_t> (blocks), bounce);
      if (result.status != kIOReturnSuccess) break;
      uint64_t usable = blocks * 4096 - skip;
      if (usable > length - result.done) usable = length - result.done;
      uint64_t copied = ops.copy (result.done, bounce + skip, usable);
      if (copied > usable) { result.status = kIOReturnIOError; break; }
      result.done += copied;
      if (copied != usable) { result.status = kIOReturnIOError; break; }
      continue;
    }
    if (write && !data) {
      if (disk.resident >= residentLimit) { result.status = kIOReturnNoMemory; break; }
      if (!table) {
        table = reinterpret_cast<uint8_t **> (ops.allocate ());
        if (!table) { result.status = kIOReturnNoMemory; break; }
        memset (table, 0, 4096);
        disk.tableBytes += 4096;
      }
      data = ops.allocate ();
      if (!data) { result.status = kIOReturnNoMemory; break; }
      result.status = ops.read (seed + page, 1, data);
      if (result.status != kIOReturnSuccess) { ops.release (data); break; }
      table[page % kTieredPagesPerTable] = data;
      disk.resident += 4096;
    }
    uint64_t n = 4096 - skip;
    if (n > length - result.done) n = length - result.done;
    uint64_t moved = write ? ops.load (result.done, data + skip, n) : ops.copy (result.done, data + skip, n);
    if (moved > n) { result.status = kIOReturnIOError; break; }
    result.done += moved;
    if (moved != n) { result.status = kIOReturnIOError; break; }
  }
  return result;
}

// Frees every overlaid page and second-level table; the caller frees the first level.
template <class Ops>
static void ufsTieredRelease (Ops &ops, UfsTieredDisk &disk)
{
  if (!disk.tables) return;
  for (uint64_t chunk = 0; chunk < disk.chunks; ++chunk) {
    uint8_t **table = disk.tables[chunk];
    if (!table) continue;
    for (uint64_t i = 0; i < kTieredPagesPerTable; ++i)
      if (table[i]) ops.release (table[i]);
    ops.release (reinterpret_cast<uint8_t *> (table));
    disk.tables[chunk] = nullptr;
  }
  disk.resident = disk.tableBytes = 0;
}

// Persistent writes share the same OS-window geometry and helpers.
#include "full_root_writes.h"
