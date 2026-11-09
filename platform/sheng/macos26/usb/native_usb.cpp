// SPDX-License-Identifier: GPL-3.0-or-later
// NativeUsb: macOS 26 bring-up of the SM8550 DWC3 USB controller, in stages.
//
// Default off: native-usb=<1..3>, hardware only (PE_vmm_present == 0).
//   1  read-only report of the clock and power state; controller registers only when their power domain and clocks are on.
//   2  votes the rails (RPMh), powers the domains, runs the clocks and resets the controller.
//   3  eUSB2 repeater and PHY, host role, xHCI reset and port power, port status polling. USB 2 only, no DMA, no ring.
//      The PHY table is never compiled in: a local packager adds /arm-io/native-usb with the property "table", accepted only
//      when its size and SHA-256 equal the pinned capture (usb_phy_table_sha256.h). Without it stage 3 stops before the PHY.
// Nothing here touches storage. A wrong outcome is left by rebooting: every register written is volatile.
//
// The register work is in usb_hw.h (host tested). The RPMh rail votes and the repeater sequence are adapted from UniXNU
// QcomDwc3.cpp commit e4efe95c, Copyright (c) 2026 UniXNU contributors, BSD-2-Clause-Patent.
#include <IOKit/IOService.h>
#include <IOKit/IOLib.h>
#include <IOKit/IODeviceTreeSupport.h>
#include <IOKit/IOMemoryDescriptor.h>
#include <kern/thread.h>
#include <libkern/libkern.h>
#include <libkern/c++/OSData.h>
#include <pexpert/pexpert.h>
#include <string.h>

#include "usb_hw.h"
#include "spmi_keys.h"
#include "usb_rpmh_names.h"
#include "QcomRpmh.h"
#include "usb_phy_table_sha256.h"

using namespace sheng_usb;

class NativeUsb : public IOService {
  OSDeclareDefaultStructors(NativeUsb)

public:
  bool start(IOService *provider) override;
  uint32_t fStage = 0;
};

OSDefineMetaClassAndStructors(NativeUsb, IOService)

namespace {

constexpr unsigned kMaxStage = 3;
constexpr uint64_t kCmdDb = 0x81c60000, kRsc = 0x17a20000;
constexpr uint32_t kCmdDbBytes = 0x20000, kRscBytes = 0x10000, kRscTcsBlock = 0xd00;
constexpr unsigned kOwnedTcs = 2;
constexpr unsigned kBootDelayMs = 3000;
// Offsets inside the PM8550B repeater peripheral: only ever read here.
constexpr uint8_t kRepeaterStatusOffset = 0x08, kRepeaterEnableOffset = 0x46;

struct Window {
  IOMemoryDescriptor *descriptor = nullptr;
  IOMemoryMap *map = nullptr;
  volatile uint32_t *va = nullptr;
  uint64_t pa = 0;
  uint64_t bytes = 0;
};

bool mapWindow(Window &window, uint64_t pa, uint64_t bytes)
{
  window.descriptor = IOMemoryDescriptor::withPhysicalAddress(pa, bytes, kIODirectionInOut);
  window.map = window.descriptor ? window.descriptor->createMappingInTask(kernel_task, 0, kIOMapAnywhere | kIOMapInhibitCache) : nullptr;
  window.va = window.map ? reinterpret_cast<volatile uint32_t *>(window.map->getVirtualAddress()) : nullptr;
  window.pa = pa;
  window.bytes = bytes;
  return window.va != nullptr;
}

void unmapWindow(Window &window)
{
  OSSafeReleaseNULL(window.map);
  OSSafeReleaseNULL(window.descriptor);
  window.va = nullptr;
}

struct KernelBus final : Bus {
  Window gcc, tcsr, core, phy;
  volatile uint32_t *locate(uint64_t pa) const
  {
    const Window *const windows[] = {&gcc, &tcsr, &core, &phy};
    for (const Window *w : windows)
      if (w->va && pa >= w->pa && pa + 4 <= w->pa + w->bytes)
        return w->va + (pa - w->pa) / 4;
    return nullptr;
  }
  uint32_t read(uint64_t pa) override
  {
    volatile uint32_t *at = locate(pa);
    return at ? *at : 0;
  }
  void write(uint64_t pa, uint32_t value) override
  {
    volatile uint32_t *at = locate(pa);
    if (at) {
      *at = value;
      __asm__ volatile("dsb sy" ::: "memory");
    }
  }
  void delayMicroseconds(unsigned us) override { IODelay(us); }
  void sleepMilliseconds(unsigned ms) override { IOSleep(ms); }
};

// SPMI arbiter v7: the observer (read) path of spmi_keys.h.
struct SpmiIo final : sheng_keys::Mmio {
  Window coreWindow, cnfgWindow, obsWindow;
  uint32_t core(uint32_t o) override { return coreWindow.va[o / 4]; }
  uint32_t cnfg(uint32_t o) override { return cnfgWindow.va[o / 4]; }
  uint32_t readObs(uint32_t o) override { return obsWindow.va[o / 4]; }
  void writeObs(uint32_t o, uint32_t v) override
  {
    __asm__ volatile("dsb sy" ::: "memory");
    obsWindow.va[o / 4] = v;
    __asm__ volatile("dsb sy" ::: "memory");
  }
  void delayMicroseconds(unsigned us) override { IODelay(us); }
};

struct Pmic final : RepeaterPort {
  SpmiIo io;
  sheng_keys::Arbiter arbiter;
  int apid = -1;
  bool ready = false;

  bool open()
  {
    if (!mapWindow(io.coreWindow, kSpmiCore, kSpmiCoreBytes) || !mapWindow(io.cnfgWindow, kSpmiCnfg, kSpmiCnfgBytes) ||
        !mapWindow(io.obsWindow, kSpmiObs, kSpmiObsBytes))
      return false;
    arbiter.io = &io;
    arbiter.coreBytes = kSpmiCoreBytes;
    arbiter.cnfgBytes = kSpmiCnfgBytes;
    arbiter.obsBytes = kSpmiObsBytes;
    if (sheng_keys::probe(arbiter) != sheng_keys::Status::ok)
      return false;
    apid = sheng_keys::findApid(arbiter, kRepeaterSid, kRepeaterPeripheral);
    ready = apid >= 0;
    return ready;
  }

  // A byte of the repeater; -1 when the read failed.
  int repeaterRead(uint8_t offset)
  {
    uint8_t value = 0;
    if (!ready || sheng_keys::readByte(arbiter, apid, static_cast<uint16_t>((kRepeaterPeripheral << 8) | offset), &value) != sheng_keys::Status::ok)
      return -1;
    return value;
  }

  int read(uint8_t offset) override { return repeaterRead(offset); }
  void delayMicroseconds(unsigned us) override { IODelay(us); }

  // One byte through this EE's channel of the arbiter (spmi-pmic-arb v7, opcode 0: extended write).
  bool write(uint8_t offset, uint8_t value) override
  {
    if (!ready)
      return false;
    Window channel;
    if (!mapWindow(channel, kSpmiChn + kSpmiChnStride * static_cast<uint32_t>(apid), kSpmiChnStride))
      return false;
    channel.va[0x10 / 4] = value;
    __asm__ volatile("dsb sy" ::: "memory");
    channel.va[0] = static_cast<uint32_t>(offset) << 4;
    bool ok = false;
    for (unsigned i = 0; i < 1000; ++i) {
      const uint32_t status = channel.va[0x08 / 4];
      if (status) {
        ok = status == 1;
        break;
      }
      IODelay(10);
    }
    unmapWindow(channel);
    return ok;
  }
};

// The local-only table from the device tree, checked against the compiled-in size and digest. The copy lives for the boot.
bool loadPhyTable(PhyTable &table)
{
  IORegistryEntry *node = IORegistryEntry::fromPath("/arm-io/native-usb", gIODTPlane);
  if (!node) {
    IOLog("NativeUsb: no /arm-io/native-usb node: stage 3 needs the packager's --usb-phy-table\n");
    return false;
  }
  bool ok = false;
  OSData *blob = OSDynamicCast(OSData, node->getProperty("table"));
  if (blob && phyTableAccepted(static_cast<const uint8_t *>(blob->getBytesNoCopy()), blob->getLength())) {
    uint8_t *copy = static_cast<uint8_t *>(IOMalloc(blob->getLength()));
    if (copy) {
      memcpy(copy, blob->getBytesNoCopy(), blob->getLength());
      ok = parsePhyTable(copy, blob->getLength(), table);
    }
  }
  node->release();
  IOLog("NativeUsb: PHY table %s\n", ok ? "accepted" : "absent or rejected");
  return ok;
}

bool voteRails(KernelBus &)
{
  Window dbWindow, rsc;
  uint8_t *db = static_cast<uint8_t *>(IOMalloc(kCmdDbBytes));
  if (!db)
    return false;
  bool ok = mapWindow(dbWindow, kCmdDb, kCmdDbBytes) && mapWindow(rsc, kRsc, kRscBytes);
  if (ok) {
    for (uint32_t i = 0; i < kCmdDbBytes; i += 4) {
      const uint32_t word = dbWindow.va[i / 4];
      memcpy(db + i, &word, 4);
    }
    const QcomRscLayout layout = qcom_rsc_layout(rsc.va[0]);
    const uint32_t tcsStatus = (kRscTcsBlock + layout.tcs_stride * kOwnedTcs + layout.status) / 4;
    // TCS 2 is shared with the keyboard cover's rail vote and no common lock exists, so wait for it to go idle; a TCS that never
    // goes idle stops this stage rather than overwriting another request. The register reads non-zero when idle (rpmh-rsc.c).
    auto idle = [&]() {
      for (unsigned tries = 0; tries < 200; ++tries) {
        if (rsc.va[tcsStatus])
          return true;
        IOSleep(1);
      }
      IOLog("NativeUsb: TCS %u busy, status %08x\n", kOwnedTcs, rsc.va[tcsStatus]);
      return false;
    };
    auto send = [&](uint32_t address, uint32_t value) {
      if (!ok)
        return;
      if (!idle()) {
        ok = false;
        return;
      }
      QcomRpmhWrite writes[16];
      unsigned n = qcom_rpmh_active_request(&layout, kOwnedTcs, &address, &value, 1, writes, 16);
      for (unsigned i = 0; i < n; ++i)
        rsc.va[(kRscTcsBlock + writes[i].offset) / 4] = writes[i].value;
      bool done = false;
      for (unsigned tries = 0; tries < 20 && !done; ++tries) {
        IOSleep(1);
        done = rsc.va[(kRscTcsBlock + layout.irq_status) / 4] & (1u << kOwnedTcs);
      }
      if (!done)
        IOLog("NativeUsb: no completion for vote at %05x\n", address);
      n = qcom_rpmh_finish(&layout, kOwnedTcs, writes, 16);
      for (unsigned i = 0; i < n; ++i)
        rsc.va[(kRscTcsBlock + writes[i].offset) / 4] = writes[i].value;
    };
    static const struct {
      const char *name;
      uint32_t mv;
    } rails[] = {{"ldoe1", 880}, {"ldoe3", 1200}, {"ldob15", 1800}, {"ldob5", 3104}, {"ldof3", 912}};
    for (const auto &rail : rails) {
      QcomRpmhResource resource;
      if (qcom_cmd_db_find(db, kCmdDbBytes, rail.name, &resource)) {
        IOLog("NativeUsb: rail %s not in the command database\n", rail.name);
        ok = false;
        break;
      }
      send(resource.address + 8, 7);
      send(resource.address, rail.mv);
      send(resource.address + 4, 1);
    }
    QcomRpmhResource resource;
    if (ok && !qcom_cmd_db_find(db, kCmdDbBytes, "xo.lvl", &resource))
      send(resource.address, 3);
    else if (ok)
      ok = false;
    if (ok && !qcom_cmd_db_find(db, kCmdDbBytes, "SN2", &resource)) {
      uint32_t vote = 0x400;
      if (resource.level_count >= 3) {
        const uint64_t unit = resource.levels[0] | (uint32_t)resource.levels[1] << 16;
        if (unit && resource.levels[2]) {
          const uint64_t x = (((2500000ull * resource.levels[2] + 15) / 16) * 1000 + unit - 1) / unit;
          vote = x < 1 ? 1 : x > 0x3fff ? 0x3fff : (uint32_t)x;
        }
      }
      send(resource.address, 1u << 30 | 1u << 29 | vote << 14 | vote);
    } else if (ok) {
      ok = false;
    }
  }
  unmapWindow(dbWindow);
  unmapWindow(rsc);
  IOFree(db, kCmdDbBytes);
  return ok;
}

void report(KernelBus &bus, Pmic &pmic, const char *when)
{
  const Power p = readPower(bus);
  const Core c = readCore(bus, p);
  IOLog("NativeUsb %s: gdsc usb30=%08x phy=%08x clk m=%08x s=%08x u=%08x\n", when, p.gdscUsb30, p.gdscPhy, p.master, p.sleep, p.mockUtmi);
  IOLog("NativeUsb %s: clk cfg=%08x agg=%08x aux=%08x com=%08x pipe=%08x\n", when, p.cfgNoc, p.aggr, p.phyAux, p.phyCom, p.phyPipe);
  IOLog("NativeUsb %s: rcg m=%08x cfg=%08x mock=%08x tcsr u2=%08x u3=%08x\n", when, p.rcgMaster, p.rcgMasterCfg, p.rcgMock, p.tcsrUsb2, p.tcsrUsb3);
  if (pmic.ready)
    IOLog("NativeUsb %s: repeater status=%d enable=%d\n", when, pmic.repeaterRead(kRepeaterStatusOffset), pmic.repeaterRead(kRepeaterEnableOffset));
  if (!c.accessible) {
    IOLog("NativeUsb %s: core registers skipped (power domain or clocks off)\n", when);
    return;
  }
  IOLog("NativeUsb %s: core id=%08x gctl=%08x u2=%08x u3=%08x hw0=%08x hw3=%08x\n", when, c.gsnpsid, c.gctl, c.usb2, c.usb3, c.hw0, c.hw3);
  IOLog("NativeUsb %s: core dctl=%08x dsts=%08x scratch hs=%08x ss=%08x gen=%08x\n", when, c.dctl, c.dsts, c.scratchHs, c.scratchSs, c.scratchGeneral);
  if (c.host)
    IOLog("NativeUsb %s: xhci cap=%x hcs1=%08x cmd=%08x sts=%08x port0=%08x port1=%08x\n", when, c.capLength, c.hcs1, c.usbCmd, c.usbSts,
          c.ports[0], c.ports[1]);
}

void bringUpThread(void *self, wait_result_t)
{
  NativeUsb *driver = static_cast<NativeUsb *>(self);
  const uint32_t stage = driver->fStage;
  KernelBus bus;
  Pmic pmic;
  IOSleep(kBootDelayMs);
  if (!mapWindow(bus.gcc, kGcc, kGccBytes) || !mapWindow(bus.tcsr, kTcsr, kTcsrBytes) || !mapWindow(bus.core, kCore, kCoreBytes) ||
      !mapWindow(bus.phy, kPhy, kPhyBytes)) {
    IOLog("NativeUsb: cannot map the controller windows\n");
    return;
  }
  const bool pmicReady = pmic.open();
  if (!pmicReady)
    IOLog("NativeUsb: PMIC arbiter or repeater channel not available\n");
  report(bus, pmic, "t0");
  Step failed = Step::none;
  if (stage >= 2) {
    if (!voteRails(bus))
      failed = Step::rails;
    if (failed == Step::none)
      failed = powerUp(bus);
    report(bus, pmic, "powered");
  }
  uint32_t cap = 0;
  if (stage >= 3 && failed == Step::none) {
    PhyTable table;
    if (!loadPhyTable(table))
      failed = Step::phy;
    if (failed == Step::none && (!pmicReady || !repeaterInit(pmic, table)))
      failed = Step::repeater;
    if (failed == Step::none) {
      applyPhyTable(bus, table);
      IOSleep(2);
      selectUtmi(bus);
      setHostRole(bus);
      if (!repeaterHost(pmic, table))
        failed = Step::role;
    }
    if (failed == Step::none)
      failed = xhciResetAndPower(bus, &cap);
    report(bus, pmic, "host");
  }
  IOLog("NativeUsb: stage %u %s%s\n", stage, failed == Step::none ? "reached" : "stopped at ", failed == Step::none ? "" : stepName(failed));
  if (failed != Step::none || stage < 3) {
    for (unsigned second = 10; second <= 60; second += 10) {
      IOSleep(10000);
      char when[8];
      snprintf(when, sizeof when, "t%us", second);
      report(bus, pmic, when);
    }
    return;
  }
  // Port status polling: every 200 ms, printed on any change, with a heartbeat. A device reaches the port only if it has VBUS.
  uint32_t last[kPortsReported] = {};
  unsigned attached = 0, polls = 0;
  const uint64_t op = kCore + cap;
  const uint32_t portCount = (bus.read(kCore + 4) >> 24) & 0xff;
  while (polls < 9000) {  // 30 minutes
    for (unsigned i = 0; i < kPortsReported && i < portCount; ++i) {
      const uint32_t value = bus.read(op + portsc(i));
      if ((value ^ last[i]) & (kPortConnected | kPortEnabled | kPortPower | (0xfu << 5) | (0xfu << 10) | kPortChangeBits)) {
        IOLog("NativeUsb port%u: portsc=%08x %s speed=%s link=%u\n", i, value, (value & kPortConnected) ? "connected" : "empty",
              speedName(portSpeed(value)), portLinkState(value));
        if ((value & kPortConnected) && !(last[i] & kPortConnected))
          ++attached;
        last[i] = value;
      }
    }
    if (polls % 150 == 0)
      IOLog("NativeUsb heartbeat: polls=%u attached=%u sts=%08x\n", polls, attached, bus.read(op + kUsbStsOffset));
    IOSleep(200);
    ++polls;
  }
}

}  // namespace

bool NativeUsb::start(IOService *provider)
{
  uint32_t stage = 0;
  if (!PE_parse_boot_argn("native-usb", &stage, sizeof(stage)) || stage < 1 || stage > kMaxStage)
    return false;
  if (PE_vmm_present != 0) {
    IOLog("NativeUsb: the controller is not driven in a VM\n");
    return false;
  }
  if (!IOService::start(provider))
    return false;
  fStage = stage;
  IOLog("NativeUsb: stage %u starting\n", stage);
  thread_t thread = nullptr;
  if (kernel_thread_start(bringUpThread, this, &thread) == KERN_SUCCESS)
    thread_deallocate(thread);
  else
    IOLog("NativeUsb: cannot start the bring-up thread\n");
  registerService();
  return true;
}
