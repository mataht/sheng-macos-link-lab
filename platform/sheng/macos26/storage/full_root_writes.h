// SPDX-License-Identifier: MIT
#pragma once
// Persistent writes for the full-system root window (development mode only, default off).
//
// The block device reports 512-byte sectors on top of 4096-byte UFS blocks. Whole UFS blocks are written in
// batches of up to kDataMax; a request that starts or ends inside a UFS block reads that block first and
// writes it back with the new bytes (read-modify-write). Every write stays inside the window; the transport's
// own write guard checks the same range again. The first failed media write latches the window read-only
// (fail-stop): later writes are refused instead of continuing on a medium in an unknown state.
//
// Requires RootWindowRead.h (UfsWindowResult, UfsWindowGuard, ufsWindowGeometry) and kDataMax.
// Ops: lock/unlock (serializes the window), read(lba, blocks, out), write(lba, blocks, src, fua),
// load(offset, out, n) copies the caller's bytes into out.
#include <stdint.h>
#include <string.h>

struct UfsRootWrites {
  bool enabled;    // boot argument accepted for this window
  bool failed;     // latched after the first media write failure
  uint64_t writes, blocks, readModifyWrites;
};

template <class Ops>
static UfsWindowResult ufsWriteWindow (Ops &ops, UfsRootWrites &state, uint64_t absolute, uint64_t bytes,
                                       uint64_t block, uint64_t count, bool fua, uint8_t *bounce)
{
  if (!state.enabled) return { kIOReturnNotWritable, 0 };
  if (!bounce || !ufsWindowGeometry (absolute, bytes) || (bytes & 4095) || !count || block >= bytes / 512 ||
      count > bytes / 512 - block)
    return { kIOReturnBadArgument, 0 };
  UfsWindowGuard<Ops> guard (ops);
  if (state.failed) return { kIOReturnNotWritable, 0 };
  const uint64_t base = absolute / 4096, length = count * 512, batch = kDataMax / 4096;
  UfsWindowResult result = { kIOReturnSuccess, 0 };
  while (result.done < length) {
    uint64_t at = block * 512 + result.done, page = at / 4096, skip = at & 4095;
    uint64_t remaining = length - result.done;
    uint64_t blocks, usable;
    if (skip || remaining < 4096) {
      // Partial UFS block: merge the caller's bytes into the current block contents.
      blocks = 1;
      usable = 4096 - skip < remaining ? 4096 - skip : remaining;
      result.status = ops.read (base + page, 1, bounce);
      if (result.status != kIOReturnSuccess) break;
      ++state.readModifyWrites;
    } else {
      blocks = remaining / 4096 < batch ? remaining / 4096 : batch;
      usable = blocks * 4096;
    }
    uint64_t loaded = ops.load (result.done, bounce + skip, usable);
    if (loaded != usable) { result.status = kIOReturnIOError; break; }
    result.status = ops.write (base + page, static_cast<uint32_t> (blocks), bounce, fua);
    if (result.status != kIOReturnSuccess) { state.failed = true; break; }
    ++state.writes;
    state.blocks += blocks;
    result.done += usable;
  }
  return result;
}
