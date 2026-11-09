// SPDX-License-Identifier: GPL-3.0-or-later
// NativeWifi: macOS 26 front end for the WCN7850 (Wi-Fi) path of sheng.
//
// Default off, hardware only (PE_vmm_present == 0). Boot argument sheng-wifi=<stage>:
//   1  read-only snapshot. Upstream UniXNU-sheng package/WifiProbe.cpp (the file at our pinned commit, compiled unchanged) reads the PCIe0
//      clock votes, GDSC and branch states, three pins, and (only if the block is powered and clocked) the controller; this file adds the
//      WCN7850 power inputs through the SPMI observer (wcn_state.h) and the pin states of Wi-Fi and Bluetooth. Nothing is written.
//   2  stage 1, then the upstream power-up: sleep clock pin, six RPMh rails, WLAN_EN, PCIe0 GDSCs, clocks, QMP PHY, link training, and a
//      configuration read of the endpoint ID (wcn_bringup.h). Changes hardware state until the next reset; writes nothing to storage.
//   3  stage 2, then BAR0, memory decoding, and a read of the MHI registers. No firmware, no DMA.
// The reporter prints each row at t=0, 20, 60 and 120 s and whenever a row changes, so photos of the boot text record the state.
#include <IOKit/IOLib.h>
#include <IOKit/IOMemoryDescriptor.h>
#include <IOKit/IOService.h>
#include <kern/thread.h>
#include <pexpert/pexpert.h>
#include <string.h>
#include "spmi_keys.h"
#include "wcn_state.h"
#include "wcn_bringup.h"

extern "C" {
void sheng_wifi_probe(void);
extern volatile uint32_t native_wifi_stats[10];
}

namespace {

// A full system barrier on the device; the host fixture of the tests builds this file for its own architecture.
inline void wifiBarrier()
{
#if defined(__arm64__) || defined(__aarch64__)
  __asm__ volatile("dsb sy" ::: "memory");
#else
  __asm__ volatile("" ::: "memory");
#endif
}

constexpr unsigned kMaxStage = 3;
constexpr uint64_t kCorePhysical = 0x0c400000, kCoreBytes = 0x3000;
constexpr uint64_t kCnfgPhysical = 0x0c42d000, kCnfgBytes = 0x4000;
constexpr uint64_t kObsPhysical = 0x0c440000, kObsBytes = 0x8000;
constexpr uint32_t kUartBranchHalt = 0x1e754, kUartVote = 0x52010;
// The rows, in the order they print: clocks, link, two rows of pins, the rails, the sleep clock pin, the UART14 branch of Bluetooth.
constexpr unsigned kRowClocks = 0, kRowLink = 1, kRowPins = 2, kRowRails = kRowPins + 2, kRowSleepClock = kRowRails + sheng_wcn::kRailCount,
                   kRowUart = kRowSleepClock + 1, kRows = kRowUart + 1;
constexpr unsigned kWaitForProbeMs = 2000;

struct Region {
  IOMemoryDescriptor *descriptor = nullptr;
  IOMemoryMap *map = nullptr;
  volatile uint32_t *base = nullptr;
  bool open(uint64_t physical, uint64_t bytes)
  {
    descriptor = IOMemoryDescriptor::withPhysicalAddress(physical, bytes, kIODirectionInOut);
    map = descriptor ? descriptor->createMappingInTask(kernel_task, 0, kIOMapAnywhere | kIOMapInhibitCache) : nullptr;
    base = map ? reinterpret_cast<volatile uint32_t *>(map->getVirtualAddress()) : nullptr;
    return base != nullptr;
  }
  void close()
  {
    OSSafeReleaseNULL(map);
    OSSafeReleaseNULL(descriptor);
    base = nullptr;
  }
};

struct SpmiIo final : sheng_keys::Mmio {
  Region coreRegion, cnfgRegion, obsRegion;
  uint32_t core(uint32_t o) override { return coreRegion.base[o / 4]; }
  uint32_t cnfg(uint32_t o) override { return cnfgRegion.base[o / 4]; }
  uint32_t readObs(uint32_t o) override { return obsRegion.base[o / 4]; }
  void writeObs(uint32_t o, uint32_t v) override
  {
    wifiBarrier();
    obsRegion.base[o / 4] = v;
    wifiBarrier();
  }
  void delayMicroseconds(unsigned us) override { IODelay(us); }
};

class Hardware final : public sheng_wcn::Hw {
  Region fRegions[sheng_wcn::kBlockCount];
  Region fChannel;
  int fChannelApid = -1;

public:
  bool open(sheng_wcn::Block block)
  {
    return fRegions[block].open(sheng_wcn::kBlocks[block].physical, sheng_wcn::kBlocks[block].bytes);
  }
  bool openSpmiChannel(int apid) override
  {
    if (fChannelApid == apid)
      return true;
    fChannel.close();
    fChannelApid = fChannel.open(sheng_wcn::kBlocks[sheng_wcn::kSpmiChannel].physical + 0x1000ull * static_cast<uint32_t>(apid), 0x1000) ? apid : -1;
    return fChannelApid == apid;
  }
  bool openBar(uint64_t physical, uint32_t bytes) override { return fRegions[sheng_wcn::kBar].open(physical, bytes); }
  uint32_t read32(sheng_wcn::Block block, uint32_t offset) override
  {
    if (block == sheng_wcn::kSpmiChannel)
      return fChannel.base[(offset & 0xfff) / 4];
    return fRegions[block].base[offset / 4];
  }
  void write32(sheng_wcn::Block block, uint32_t offset, uint32_t value) override
  {
    if (block == sheng_wcn::kSpmiChannel)
      fChannel.base[(offset & 0xfff) / 4] = value;
    else
      fRegions[block].base[offset / 4] = value;
    wifiBarrier();
  }
  void sleepMs(unsigned ms) override { IOSleep(ms); }
  void delayUs(unsigned us) override { IODelay(us); }
  void barrier() override { wifiBarrier(); }
};

}  // namespace

class NativeWifi : public IOService {
  OSDeclareDefaultStructors(NativeWifi)

  unsigned fStage = 0;
  SpmiIo fIo;
  sheng_wcn::Arbiter fArbiter;
  bool fSpmi = false;
  Hardware fHw;
  bool fHaveGpioAndClocks = false;

  static void reportThread(void *self, wait_result_t);
  void collect(char rows[kRows][sheng_wcn::kRowBytes]);
  void readBlocks();
  void runBringUp();

public:
  bool start(IOService *provider) override;
};

OSDefineMetaClassAndStructors(NativeWifi, IOService)

void NativeWifi::readBlocks()
{
  fHaveGpioAndClocks = fHw.open(sheng_wcn::kGcc) && fHw.open(sheng_wcn::kTlmm);
  if (!fHaveGpioAndClocks)
    IOLog("NativeWifi: cannot map GCC or TLMM\n");
}

void NativeWifi::collect(char rows[kRows][sheng_wcn::kRowBytes])
{
  uint32_t stats[sheng_wcn::kWifiStatsWords];
  for (unsigned i = 0; i < sheng_wcn::kWifiStatsWords; ++i)
    stats[i] = native_wifi_stats[i];
  sheng_wcn::formatClocks(rows[kRowClocks], sheng_wcn::kRowBytes, stats);
  sheng_wcn::formatLink(rows[kRowLink], sheng_wcn::kRowBytes, stats);
  if (fHaveGpioAndClocks) {
    sheng_wcn::PinState pins[sheng_wcn::kPinCount];
    for (unsigned i = 0; i < sheng_wcn::kPinCount; ++i) {
      pins[i].config = fHw.read32(sheng_wcn::kTlmm, sheng_wcn::kPins[i] * 0x1000u);
      pins[i].io = fHw.read32(sheng_wcn::kTlmm, sheng_wcn::kPins[i] * 0x1000u + 4);
    }
    sheng_wcn::formatPins(rows[kRowPins], sheng_wcn::kRowBytes, 0, pins);
    sheng_wcn::formatPins(rows[kRowPins + 1], sheng_wcn::kRowBytes, sheng_wcn::kPinsPerRow, pins);
  } else {
    snprintf(rows[kRowPins], sheng_wcn::kRowBytes, "pins unavailable");
    snprintf(rows[kRowPins + 1], sheng_wcn::kRowBytes, "pins unavailable");
  }
  for (unsigned i = 0; i < sheng_wcn::kRailCount; ++i) {
    if (!fSpmi) {
      snprintf(rows[kRowRails + i], sheng_wcn::kRowBytes, "%s spmi unavailable", sheng_wcn::kRails[i].name);
      continue;
    }
    sheng_wcn::RailState rail;
    (void)sheng_wcn::readRail(fArbiter, sheng_wcn::kRails[i], &rail);
    sheng_wcn::formatRail(rows[kRowRails + i], sheng_wcn::kRowBytes, sheng_wcn::kRails[i], rail);
  }
  if (fSpmi) {
    sheng_wcn::SleepClockState clock;
    (void)sheng_wcn::readSleepClock(fArbiter, &clock);
    sheng_wcn::formatSleepClock(rows[kRowSleepClock], sheng_wcn::kRowBytes, clock);
  } else {
    snprintf(rows[kRowSleepClock], sheng_wcn::kRowBytes, "sleepclk spmi unavailable");
  }
  if (fHaveGpioAndClocks)
    snprintf(rows[kRowUart], sheng_wcn::kRowBytes, "uart14 vote %08x halt %08x",
             fHw.read32(sheng_wcn::kGcc, kUartVote), fHw.read32(sheng_wcn::kGcc, kUartBranchHalt));
  else
    snprintf(rows[kRowUart], sheng_wcn::kRowBytes, "uart14 unavailable");
}

void NativeWifi::runBringUp()
{
  // Mapped here, only when a bring-up stage was asked for.
  const sheng_wcn::Block blocks[] = {sheng_wcn::kTcsr, sheng_wcn::kParf, sheng_wcn::kDbi, sheng_wcn::kPhy,
                                     sheng_wcn::kAtu, sheng_wcn::kConfig, sheng_wcn::kRsc};
  bool mapped = fHaveGpioAndClocks && fSpmi;
  for (sheng_wcn::Block block : blocks)
    mapped = mapped && fHw.open(block);
  if (!mapped) {
    IOLog("NativeWifi: stage %u: cannot map the PCIe0 and RSC blocks\n", fStage);
    return;
  }
  uint8_t *db = static_cast<uint8_t *>(IOMalloc(sheng_wcn::kCmdDbBytes));
  if (!db) {
    IOLog("NativeWifi: stage %u: no memory for the command database copy\n", fStage);
    return;
  }
  Region cmdDb;
  if (!cmdDb.open(sheng_wcn::kCmdDbPhysical, sheng_wcn::kCmdDbBytes)) {
    IOLog("NativeWifi: stage %u: cannot map the command database\n", fStage);
    IOFree(db, sheng_wcn::kCmdDbBytes);
    return;
  }
  for (uint32_t i = 0; i < sheng_wcn::kCmdDbBytes; i += 4) {
    const uint32_t w = cmdDb.base[i / 4];
    memcpy(db + i, &w, 4);
  }
  cmdDb.close();
  static sheng_wcn::Report report;
  sheng_wcn::bringUp(fHw, fArbiter, db, fStage, report);
  IOFree(db, sheng_wcn::kCmdDbBytes);
  char row[sheng_wcn::kRowBytes];
  for (unsigned i = 0; i < sheng_wcn::kBringupRows; ++i) {
    sheng_wcn::formatBringup(row, sizeof row, i, report);
    IOLog("NativeWifi bringup %u: %s\n", i, row);
  }
}

void NativeWifi::reportThread(void *self, wait_result_t)
{
  NativeWifi *wifi = static_cast<NativeWifi *>(self);
  // Upstream's own thread runs first; it fills native_wifi_stats quickly and then ends.
  for (unsigned waited = 0; waited < kWaitForProbeMs && !native_wifi_stats[0]; waited += 10)
    IOSleep(10);
  wifi->readBlocks();
  static char rows[kRows][sheng_wcn::kRowBytes];
  static char last[kRows][sheng_wcn::kRowBytes];
  wifi->collect(rows);
  for (unsigned i = 0; i < kRows; ++i) {
    IOLog("NativeWifi t0 R%02u: %s\n", i, rows[i]);
    memcpy(last[i], rows[i], sheng_wcn::kRowBytes);
  }
  if (wifi->fStage >= 2)
    wifi->runBringUp();
  for (unsigned second = 1; second <= 120; ++second) {
    IOSleep(1000);
    wifi->collect(rows);
    const bool all = second == 20 || second == 60 || second == 120;
    for (unsigned i = 0; i < kRows; ++i) {
      if (all || strncmp(rows[i], last[i], sheng_wcn::kRowBytes) != 0)
        IOLog("NativeWifi t%u R%02u: %s\n", second, i, rows[i]);
      memcpy(last[i], rows[i], sheng_wcn::kRowBytes);
    }
  }
}

bool NativeWifi::start(IOService *provider)
{
  uint32_t stage = 0;
  if (!PE_parse_boot_argn("sheng-wifi", &stage, sizeof(stage)) || stage < 1 || stage > kMaxStage)
    return false;
  if (PE_vmm_present != 0) {
    IOLog("NativeWifi: the WCN7850 path is not driven in a VM\n");
    return false;
  }
  if (!IOService::start(provider))
    return false;
  fStage = stage;
  if (fIo.coreRegion.open(kCorePhysical, kCoreBytes) && fIo.cnfgRegion.open(kCnfgPhysical, kCnfgBytes) &&
      fIo.obsRegion.open(kObsPhysical, kObsBytes)) {
    fArbiter.io = &fIo;
    fArbiter.coreBytes = kCoreBytes;
    fArbiter.cnfgBytes = kCnfgBytes;
    fArbiter.obsBytes = kObsBytes;
    fArbiter.ee = 0;
    fSpmi = sheng_keys::probe(fArbiter) == sheng_keys::Status::ok;
    IOLog("NativeWifi: SPMI arbiter version %#x, %u APIDs, %s\n", fArbiter.version, fArbiter.apids, fSpmi ? "usable" : "unusable");
  } else {
    IOLog("NativeWifi: cannot map the SPMI arbiter; the power inputs are not reported\n");
    fIo.coreRegion.close(), fIo.cnfgRegion.close(), fIo.obsRegion.close();
  }
  sheng_wifi_probe();
  registerService();
  IOLog("NativeWifi: stage %u started (1 read-only, 2 power and link, 3 endpoint registers)\n", stage);
  // The driver cannot unload; the thread holds the provider for its lifetime.
  retain();
  thread_t reporter = nullptr;
  if (kernel_thread_start(reportThread, this, &reporter) == KERN_SUCCESS)
    thread_deallocate(reporter);
  else {
    release();
    IOLog("NativeWifi: cannot start the reporter\n");
  }
  return true;
}
