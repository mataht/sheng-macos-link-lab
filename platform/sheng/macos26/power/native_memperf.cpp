// SPDX-License-Identifier: GPL-3.0-or-later
// NativeMemPerf: read-only look at why memory and framebuffer writes may be slow on the sheng tablet.
//
// Default off: sheng-memperf=1, hardware only (PE_vmm_present == 0). It issues no register writes at all. It reports the EPSS L3
// vote and table, the AOP command database records of the bus resources, the last commands programmed in the apps RSC's TCSes,
// and three rounds of a memory benchmark on its own buffers: a 64 MiB cached one (larger than the L3, so it reaches DDR) and 4 MiB
// through Device and write-combined mappings.
//
// The Device and write-combined views are ml_io_map mappings of the buffer's physical address, the way the boot console maps the
// framebuffer. Mapping the IOBufferMemoryDescriptor again does not work: in kernel_task XNU returns the existing cached address
// (IOMemoryDescriptor doMap, "mapping source == dest"), which is what boot 05 measured, and kIOMapUnique would change the cache
// mode of the buffer's whole VM object.
//
// Physical regions (sheng device tree, offline check): apps RSC DRV2 0x17a20000 (TCS block at +0xd00, 8 TCSes: 3 active, 2 sleep,
// 2 wake, 1 fast path), L3 0x17d90000, AOP cmd-db 0x81c60000 (0x20000). rpmh_readout.h has the formats and their sources.
#include <IOKit/IOBufferMemoryDescriptor.h>
#include <IOKit/IOLib.h>
#include <IOKit/IOMemoryDescriptor.h>
#include <IOKit/IOService.h>
#include <kern/clock.h>
#include <kern/thread.h>
#include <pexpert/pexpert.h>
#include <string.h>
#include "rpmh_readout.h"

extern "C" {
vm_offset_t ml_io_map(vm_offset_t phys_addr, vm_size_t size);
vm_offset_t ml_io_map_wcomb(vm_offset_t phys_addr, vm_size_t size);
}

namespace {

using namespace sheng_memperf;

constexpr uint64_t kRscPhysical = 0x17a20000, kRscBytes = 0x10000;
constexpr uint64_t kL3Physical = 0x17d90000, kL3Bytes = 0x1000;
constexpr uint64_t kCmdDbPhysical = 0x81c60000, kCmdDbBytes = 0x20000;
constexpr uint32_t kTcsBlock = 0xd00;
constexpr unsigned kTcsCount = 8, kCommandsPerTcs = 16;
constexpr size_t kBenchBytes = 4u << 20, kLargeBytes = 64u << 20;
// All rounds come early: once WindowServer owns the screen the boot text is no longer visible.
constexpr unsigned kBenchRepeats = 3, kRounds = 3, kFirstRoundSeconds = 3, kRoundGapSeconds = 5;

const char *const kResources[] = {"MC0", "SH0", "ACV", "CN0", "MM0"};
constexpr unsigned kResourceCount = sizeof kResources / sizeof kResources[0];

struct Window {
  IOMemoryDescriptor *descriptor = nullptr;
  IOMemoryMap *map = nullptr;
  volatile uint32_t *base = nullptr;
  bool open(uint64_t physical, uint64_t bytes)
  {
    descriptor = IOMemoryDescriptor::withPhysicalAddress(physical, bytes, kIODirectionIn);
    map = descriptor ? descriptor->createMappingInTask(kernel_task, 0, kIOMapAnywhere | kIOMapInhibitCache) : nullptr;
    base = map ? reinterpret_cast<volatile uint32_t *>(map->getVirtualAddress()) : nullptr;
    return base != nullptr;
  }
  uint32_t read(uint32_t offset) { return base[offset / 4]; }
};

uint32_t mibPerSecond(size_t bytes, uint64_t nanoseconds)
{
  return nanoseconds ? static_cast<uint32_t>((static_cast<uint64_t>(bytes) * 1000000000ull / nanoseconds) >> 20) : 0;
}

uint64_t elapsedNs(uint64_t start)
{
  uint64_t now = 0, ns = 0;
  clock_get_uptime(&now);
  absolutetime_to_nanoseconds(now - start, &ns);
  return ns;
}

// Word-sized volatile accesses: a Device mapping does not allow the byte patterns memcpy would use.
uint32_t writeSpeed(volatile uint32_t *p, size_t bytes)
{
  uint64_t best = UINT64_MAX;
  for (unsigned r = 0; r < kBenchRepeats; ++r) {
    uint64_t start = 0;
    clock_get_uptime(&start);
    for (size_t i = 0; i < bytes / 4; ++i)
      p[i] = 0x5a5a0000u + r;
    __asm__ volatile("dsb sy" ::: "memory");
    const uint64_t ns = elapsedNs(start);
    if (ns < best)
      best = ns;
  }
  return mibPerSecond(bytes, best);
}

uint32_t readSpeed(volatile uint32_t *p, size_t bytes)
{
  uint64_t best = UINT64_MAX;
  uint32_t sink = 0;
  for (unsigned r = 0; r < kBenchRepeats; ++r) {
    uint64_t start = 0;
    clock_get_uptime(&start);
    for (size_t i = 0; i < bytes / 4; ++i)
      sink ^= p[i];
    const uint64_t ns = elapsedNs(start);
    if (ns < best)
      best = ns;
  }
  (void)sink;
  return mibPerSecond(bytes, best);
}

}  // namespace

class NativeMemPerf : public IOService {
  OSDeclareDefaultStructors(NativeMemPerf)

  Window fRsc, fL3, fCmdDb;
  IOBufferMemoryDescriptor *fBuffer = nullptr, *fLarge = nullptr;
  volatile uint32_t *fDevice = nullptr, *fCombined = nullptr;

  void reportL3();
  void reportResources(Resource *found);
  void reportTcs(const Resource *found);
  void benchRound(unsigned round);
  static void reportThread(void *self, wait_result_t);

public:
  bool start(IOService *provider) override;
};

OSDefineMetaClassAndStructors(NativeMemPerf, IOService)

void NativeMemPerf::reportL3()
{
  if (!fL3.base) {
    IOLog("NativeMemPerf: L3 not mapped\n");
    return;
  }
  uint32_t topKHz = 0;
  const unsigned levels = lutLevels(fL3, &topKHz);
  const uint32_t vote = fL3.read(kL3Vote);
  const uint32_t rowKHz = vote < kLutEntries ? lutKHz(fL3.read(kL3Lut + 4 * vote)) : 0;
  IOLog("NativeMemPerf: L3 enable %u, vote %u (%u MHz), LUT %u levels, top %u MHz\n", fL3.read(kL3Enable) & 1, vote, rowKHz / 1000, levels,
        topKHz / 1000);
}

void NativeMemPerf::reportResources(Resource *found)
{
  if (!fCmdDb.base) {
    IOLog("NativeMemPerf: cmd-db not mapped\n");
    return;
  }
  uint8_t *copy = static_cast<uint8_t *>(IOMalloc(kCmdDbBytes));
  if (!copy)
    return;
  for (size_t i = 0; i < kCmdDbBytes / 4; ++i) {
    const uint32_t word = fCmdDb.base[i];
    memcpy(copy + 4 * i, &word, 4);
  }
  for (unsigned i = 0; i < kResourceCount; ++i) {
    const DbStatus status = cmdDbFind(copy, kCmdDbBytes, kResources[i], &found[i]);
    IOLog("NativeMemPerf: %s status %d address %#x unit %u width %u vcd %u\n", kResources[i], static_cast<int>(status), found[i].address,
          found[i].unit, found[i].width, found[i].vcd);
  }
  IOFree(copy, kCmdDbBytes);
}

void NativeMemPerf::reportTcs(const Resource *found)
{
  if (!fRsc.base) {
    IOLog("NativeMemPerf: RSC not mapped\n");
    return;
  }
  const uint32_t id = fRsc.read(0);
  const RscLayout layout = rscLayout(id);
  IOLog("NativeMemPerf: RSC DRV2 id %#x\n", id);
  for (unsigned t = 0; t < kTcsCount; ++t) {
    const uint32_t base = kTcsBlock + layout.tcsStride * t;
    const uint32_t enable = fRsc.read(base + layout.cmdEnable);
    const uint32_t control = fRsc.read(base + layout.control), status = fRsc.read(base + layout.status);
    if (!enable && !control)
      continue;
    IOLog("NativeMemPerf: TCS%u control %#x status %#x enable %#x\n", t, control, status, enable);
    for (unsigned c = 0; c < kCommandsPerTcs; ++c) {
      if (!(enable & (1u << c)))
        continue;
      const uint32_t cmd = base + layout.cmdStride * c;
      const uint32_t address = fRsc.read(cmd + layout.cmdAddr), data = fRsc.read(cmd + layout.cmdData);
      const char *name = "";
      for (unsigned i = 0; i < kResourceCount; ++i)
        if (found[i].address && found[i].address == address)
          name = kResources[i];
      const BcmVote vote = bcmVote(data);
      IOLog("NativeMemPerf:   cmd%u addr %#x %s data %#x valid %u x %u y %u\n", c, address, name, data, vote.valid, vote.x, vote.y);
    }
  }
}

void NativeMemPerf::benchRound(unsigned round)
{
  uint32_t cachedWrite = 0, cachedRead = 0, deviceWrite = 0, deviceRead = 0, combinedWrite = 0, combinedRead = 0;
  if (fLarge) {
    volatile uint32_t *large = reinterpret_cast<volatile uint32_t *>(fLarge->getBytesNoCopy());
    cachedWrite = writeSpeed(large, kLargeBytes);
    cachedRead = readSpeed(large, kLargeBytes);
  }
  if (fDevice) {
    deviceWrite = writeSpeed(fDevice, kBenchBytes);
    deviceRead = readSpeed(fDevice, kBenchBytes);
  }
  if (fCombined) {
    combinedWrite = writeSpeed(fCombined, kBenchBytes);
    combinedRead = readSpeed(fCombined, kBenchBytes);
  }
  IOLog("NativeMemPerf: round %u MiB/s: cached 64M write %u read %u, device write %u read %u, combined write %u read %u\n", round, cachedWrite,
        cachedRead, deviceWrite, deviceRead, combinedWrite, combinedRead);
}

void NativeMemPerf::reportThread(void *self, wait_result_t)
{
  NativeMemPerf *perf = static_cast<NativeMemPerf *>(self);
  for (unsigned round = 1; round <= kRounds; ++round) {
    IOSleep((round == 1 ? kFirstRoundSeconds : kRoundGapSeconds) * 1000);
    perf->benchRound(round);
    perf->reportL3();
  }
}

bool NativeMemPerf::start(IOService *provider)
{
  uint32_t mode = 0;
  if (!PE_parse_boot_argn("sheng-memperf", &mode, sizeof(mode)) || mode != 1)
    return false;
  if (PE_vmm_present != 0) {
    IOLog("NativeMemPerf: memory performance state is not read in a VM\n");
    return false;
  }
  if (!IOService::start(provider))
    return false;
  fRsc.open(kRscPhysical, kRscBytes);
  fL3.open(kL3Physical, kL3Bytes);
  fCmdDb.open(kCmdDbPhysical, kCmdDbBytes);

  Resource found[kResourceCount];
  reportL3();
  reportResources(found);
  reportTcs(found);

  fLarge = IOBufferMemoryDescriptor::inTaskWithOptions(kernel_task, kIODirectionInOut, kLargeBytes, 0x4000);
  fBuffer = IOBufferMemoryDescriptor::inTaskWithOptions(kernel_task, static_cast<IOOptionBits>(kIODirectionInOut) | kIOMemoryPhysicallyContiguous,
                                                        kBenchBytes, 0x4000);
  const addr64_t physical = fBuffer ? fBuffer->getPhysicalSegment(0, nullptr, kIOMemoryMapperNone) : 0;
  if (physical) {
    fDevice = reinterpret_cast<volatile uint32_t *>(ml_io_map(static_cast<vm_offset_t>(physical), kBenchBytes));
    fCombined = reinterpret_cast<volatile uint32_t *>(ml_io_map_wcomb(static_cast<vm_offset_t>(physical), kBenchBytes));
  }
  IOLog("NativeMemPerf: cached 64M buffer %s, 4M device map %s, write-combined map %s\n", fLarge ? "ok" : "failed", fDevice ? "ok" : "failed",
        fCombined ? "ok" : "failed");
  if (fLarge || fDevice || fCombined) {
    retain();
    thread_t thread;
    if (kernel_thread_start(reportThread, this, &thread) == KERN_SUCCESS)
      thread_deallocate(thread);
    else
      release();
  }
  registerService();
  return true;
}
