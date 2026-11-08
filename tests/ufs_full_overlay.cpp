// SPDX-License-Identifier: GPL-3.0-or-later
// Host test of the full-system root overlay against an immutable seed.
#include <cassert>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <vector>
using IOReturn = int;
constexpr int kIOReturnSuccess = 0, kIOReturnBadArgument = 1, kIOReturnIOError = 2, kIOReturnNoMemory = 3, kIOReturnNotWritable = 4;
constexpr unsigned kDataMax = 131072;
#include "../UniXNU/drivers/NativeUfs/RootWindowRead.h"
#include "../platform/sheng/macos26/storage/full_root_overlay.h"

constexpr uint64_t kPages = 1100, kFirst = 3;
struct Ops {
  std::vector<uint8_t> seed, descriptor;
  bool locked = false, failRead = false;
  uint64_t reads = 0, maxBatch = 0, allocated = 0, released = 0, shortCopy = UINT64_MAX, allocationBudget = UINT64_MAX;
  Ops () : seed ((kFirst + kPages + 2) * 4096), descriptor (kPages * 4096) {
    for (size_t i = 0; i < seed.size (); ++i) seed[i] = uint8_t (i * 7 + i / 4096);
  }
  void lock () { assert (!locked); locked = true; }
  void unlock () { assert (locked); locked = false; }
  uint8_t *allocate () {
    assert (locked);
    if (!allocationBudget) return nullptr;
    --allocationBudget; ++allocated;
    return static_cast<uint8_t *> (malloc (4096));
  }
  void release (uint8_t *p) { ++released; free (p); }
  int read (uint64_t lba, uint32_t count, void *out) {
    assert (locked && count >= 1 && count <= kDataMax / 4096 && lba >= kFirst && lba + count <= kFirst + kPages);
    ++reads; if (count > maxBatch) maxBatch = count;
    if (failRead) return kIOReturnIOError;
    memcpy (out, seed.data () + lba * 4096, count * 4096);
    return kIOReturnSuccess;
  }
  uint64_t load (uint64_t offset, void *out, uint64_t n) {
    assert (locked); if (n > shortCopy) n = shortCopy; memcpy (out, descriptor.data () + offset, n); return n;
  }
  uint64_t copy (uint64_t offset, const void *data, uint64_t n) {
    assert (locked); if (n > shortCopy) n = shortCopy; memcpy (descriptor.data () + offset, data, n); return n;
  }
};
struct Fixture {
  Ops ops;
  uint8_t bounce[kDataMax];
  std::vector<uint8_t **> first;
  UfsTieredDisk disk;
  Fixture () : first (ufsTieredChunks (kPages * 4096)) {
    disk = { first.data (), kPages * 4096, ufsTieredChunks (kPages * 4096), 0, 0 };
  }
  ~Fixture () {
    ufsTieredRelease (ops, disk);
    assert (ops.allocated == ops.released && !disk.resident && !disk.tableBytes);
  }
  UfsWindowResult io (bool write, uint64_t block, uint64_t count, uint64_t limit = 1 << 20) {
    auto r = ufsTieredOverlayIO (ops, disk, kFirst * 4096, write, block, count, limit, bounce);
    assert (!ops.locked);
    return r;
  }
  const uint8_t *window () const { return ops.seed.data () + kFirst * 4096; }
};

int main ()
{
  {  // Unmodified reads come from the seed in batches.
    Fixture f;
    assert (f.disk.chunks == 3);
    auto r = f.io (false, 0, kPages * 8);
    assert (r.status == kIOReturnSuccess && r.done == kPages * 4096);
    assert (!memcmp (f.ops.descriptor.data (), f.window (), kPages * 4096));
    assert (f.ops.maxBatch == kDataMax / 4096 && f.ops.reads == (kPages + 31) / 32 && f.ops.allocated == 0);
  }
  {  // A write across a page boundary copies two seed pages and allocates one table lazily.
    Fixture f;
    auto before = f.ops.seed;
    memset (f.ops.descriptor.data (), 0xa7, 5120);
    auto r = f.io (true, 5 * 8 + 2, 10);
    assert (r.status == kIOReturnSuccess && r.done == 5120);
    assert (f.disk.resident == 8192 && f.disk.tableBytes == 4096);
    assert (f.io (true, 1050 * 8, 8).status == kIOReturnSuccess && f.disk.tableBytes == 8192);
    std::fill (f.ops.descriptor.begin (), f.ops.descriptor.end (), 0);
    assert (f.io (false, 0, kPages * 8).status == kIOReturnSuccess);
    for (uint64_t i = 0; i < kPages * 4096; ++i) {
      bool written = (i >= 5 * 4096 + 1024 && i < 5 * 4096 + 1024 + 5120) || (i >= 1050 * 4096 && i < 1051 * 4096);
      if (i >= 1050 * 4096 && i < 1051 * 4096) continue;  // second write copied the cleared descriptor prefix
      assert (f.ops.descriptor[i] == (written ? 0xa7 : f.window ()[i]));
    }
    assert (f.ops.seed == before);
  }
  {  // The resident limit stops a write with a truthful partial count.
    Fixture f;
    memset (f.ops.descriptor.data (), 0x5c, 8192);
    auto r = f.io (true, 0, 16, 4096);
    assert (r.status == kIOReturnNoMemory && r.done == 4096 && f.disk.resident == 4096);
  }
  {  // Invalid requests touch nothing.
    Fixture f;
    assert (f.io (false, kPages * 8, 1).status == kIOReturnBadArgument);
    assert (f.io (true, 0, UINT64_MAX).status == kIOReturnBadArgument);
    assert (f.io (true, 0, 1, 4095).status == kIOReturnBadArgument);
    assert (ufsTieredOverlayIO (f.ops, f.disk, 4096 * kFirst + 512, false, 0, 1, 4096, f.bounce).status == kIOReturnBadArgument);
    UfsTieredDisk wrong = f.disk; wrong.chunks = 2;
    assert (ufsTieredOverlayIO (f.ops, wrong, 4096 * kFirst, false, 0, 1, 4096, f.bounce).status == kIOReturnBadArgument);
    assert (f.ops.reads == 0 && f.ops.allocated == 0);
  }
  {  // A failed copy-on-write seed read releases its page and reports the error.
    Fixture f;
    f.ops.failRead = true;
    auto r = f.io (true, 0, 8);
    assert (r.status == kIOReturnIOError && r.done == 0 && f.disk.resident == 0 && f.ops.allocated == f.ops.released + 1);
  }
  {  // Allocation failure of the data page after its table keeps the table accounted for release.
    Fixture f;
    f.ops.allocationBudget = 1;
    auto r = f.io (true, 0, 8);
    assert (r.status == kIOReturnNoMemory && r.done == 0 && f.disk.tableBytes == 4096);
  }
  {  // A short caller copy is reported as an I/O error with the true count.
    Fixture f;
    f.ops.shortCopy = 100;
    auto r = f.io (false, 0, 8);
    assert (r.status == kIOReturnIOError && r.done == 100);
  }
  return 0;
}
