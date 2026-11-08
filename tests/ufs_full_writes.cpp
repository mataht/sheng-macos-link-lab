// SPDX-License-Identifier: GPL-3.0-or-later
// Host test of the full-system root persistent write path against an in-memory LUN.
#include <cassert>
#include <cstdint>
#include <cstring>
#include <vector>
using IOReturn = int;
constexpr int kIOReturnSuccess = 0, kIOReturnBadArgument = 1, kIOReturnIOError = 2, kIOReturnNotWritable = 4;
constexpr unsigned kDataMax = 131072;
#include "../UniXNU/drivers/NativeUfs/RootWindowRead.h"
#include "../platform/sheng/macos26/storage/full_root_writes.h"

constexpr uint64_t kFirst = 8, kPages = 100, kLun = kFirst + kPages + 8;
struct Ops {
  std::vector<uint8_t> lun, caller;
  bool locked = false, failWrite = false, failRead = false;
  uint64_t writes = 0, fuaWrites = 0, maxBatch = 0, shortLoad = UINT64_MAX;
  Ops () : lun (kLun * 4096), caller (kPages * 4096) {
    for (size_t i = 0; i < lun.size (); ++i) lun[i] = uint8_t (i * 5 + i / 4096);
    for (size_t i = 0; i < caller.size (); ++i) caller[i] = uint8_t (0xa0 + i % 7);
  }
  void lock () { assert (!locked); locked = true; }
  void unlock () { assert (locked); locked = false; }
  int read (uint64_t lba, uint32_t blocks, void *out) {
    assert (locked && lba >= kFirst && lba + blocks <= kFirst + kPages && blocks == 1);
    if (failRead) return kIOReturnIOError;
    memcpy (out, lun.data () + lba * 4096, 4096);
    return kIOReturnSuccess;
  }
  int write (uint64_t lba, uint32_t blocks, const void *src, bool fua) {
    // The window bounds are the only place the test LUN may change.
    assert (locked && blocks >= 1 && blocks <= kDataMax / 4096 && lba >= kFirst && lba + blocks <= kFirst + kPages);
    if (failWrite) return kIOReturnIOError;
    ++writes; fuaWrites += fua; if (blocks > maxBatch) maxBatch = blocks;
    memcpy (lun.data () + lba * 4096, src, blocks * 4096ULL);
    return kIOReturnSuccess;
  }
  uint64_t load (uint64_t offset, void *out, uint64_t n) {
    assert (locked); if (n > shortLoad) n = shortLoad;
    memcpy (out, caller.data () + offset, n); return n;
  }
};
struct Fixture {
  Ops ops;
  UfsRootWrites state = { true, false, 0, 0, 0 };
  uint8_t bounce[kDataMax];
  std::vector<uint8_t> before = ops.lun;
  UfsWindowResult io (uint64_t block, uint64_t count, bool fua = false) {
    auto r = ufsWriteWindow (ops, state, kFirst * 4096, kPages * 4096, block, count, fua, bounce);
    assert (!ops.locked);
    return r;
  }
  // Bytes outside [from, to) of the window must be unchanged; inside they equal the caller buffer.
  void expect (uint64_t from, uint64_t to) const {
    for (uint64_t i = 0; i < ops.lun.size (); ++i) {
      uint64_t w = i - kFirst * 4096;
      bool inside = i >= kFirst * 4096 && w >= from && w < to;
      assert (ops.lun[i] == (inside ? ops.caller[w - from] : before[i]));
    }
  }
};

int main ()
{
  {  // Whole blocks: batched up to kDataMax, written in place, nothing else touched.
    Fixture f;
    auto r = f.io (16, 64 * 8);
    assert (r.status == kIOReturnSuccess && r.done == 64 * 4096);
    f.expect (2 * 4096, 66 * 4096);
    assert (f.ops.maxBatch == 32 && f.ops.writes == 2 && f.state.readModifyWrites == 0 && f.state.blocks == 64);
  }
  {  // Unaligned 512-byte sectors: head and tail blocks are read-modify-write.
    Fixture f;
    auto r = f.io (3, 20);  // bytes 1536 .. 11776 spans blocks 0..2
    assert (r.status == kIOReturnSuccess && r.done == 20 * 512);
    f.expect (1536, 1536 + 20 * 512);
    assert (f.state.readModifyWrites == 2);
  }
  {  // Forced unit access is passed through.
    Fixture f;
    assert (f.io (0, 8, true).status == kIOReturnSuccess && f.ops.fuaWrites == 1);
  }
  {  // Disabled or invalid requests touch nothing.
    Fixture f;
    f.state.enabled = false;
    assert (f.io (0, 8).status == kIOReturnNotWritable);
    f.state.enabled = true;
    assert (f.io (kPages * 8, 1).status == kIOReturnBadArgument);
    assert (f.io (kPages * 8 - 4, 8).status == kIOReturnBadArgument);
    assert (ufsWriteWindow (f.ops, f.state, kFirst * 4096 + 512, kPages * 4096, 0, 1, false, f.bounce).status == kIOReturnBadArgument);
    f.expect (0, 0);
  }
  {  // A media failure latches the window read-only (fail-stop) with a truthful count.
    Fixture f;
    assert (f.io (0, 8).status == kIOReturnSuccess);
    f.ops.failWrite = true;
    auto r = f.io (8, 16);
    assert (r.status == kIOReturnIOError && r.done == 0 && f.state.failed);
    f.ops.failWrite = false;
    assert (f.io (40, 8).status == kIOReturnNotWritable);
    assert (f.ops.writes == 1);
  }
  {  // A failed read of a partial block writes nothing.
    Fixture f;
    f.ops.failRead = true;
    auto r = f.io (1, 2);
    assert (r.status == kIOReturnIOError && r.done == 0 && f.ops.writes == 0 && !f.state.failed);
    f.expect (0, 0);
  }
  {  // A short caller buffer stops before writing that block.
    Fixture f;
    f.ops.shortLoad = 100;
    auto r = f.io (0, 8);
    assert (r.status == kIOReturnIOError && r.done == 0 && f.ops.writes == 0);
  }
  return 0;
}
