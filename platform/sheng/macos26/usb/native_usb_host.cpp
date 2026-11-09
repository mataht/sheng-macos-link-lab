// SPDX-License-Identifier: GPL-3.0-or-later
// NativeUsb stage 4 (native-usb=4, built only with --build-usb-host): macOS 26's own USB host stack on the DWC3 core that
// native_usb.cpp has already put in the host role.
//
// NativeUsbXhci subclasses Apple's AppleUSBXHCI the way an Apple SoC subclass does: it supplies the register window, the
// interrupt, the 64-bit register accesses the DWC3 wants as two 32-bit ones, and keeps the controller at its on state
// (nothing here reports port power changes). NativeUsbNub is the provider: device memory 0 is the xHCI registers and
// interrupt 0 is the core's line. The stream gets a stage 1 SMMU context with a write-back identity map because Apple's
// host code does no cache maintenance.
//
// Adapted from UniXNU QcomDwc3.cpp commit e4efe95c (BSD-2-Clause-Patent, Copyright (c) 2026 UniXNU contributors). The class
// declaration comes from OriginalUsbXhciABI.h, generated from the paired 25F71 kernel collection: opaque method names, each with
// the original slot's pointer-authentication discriminator.
#include <IOKit/IOBufferMemoryDescriptor.h>
#include <IOKit/IODeviceMemory.h>
#include <IOKit/IOLib.h>
#include <IOKit/IOPlatformExpert.h>
#include <IOKit/IOService.h>
#include <kern/thread.h>
#include <libkern/OSKextLib.h>
#include <libkern/c++/OSArray.h>
#include <libkern/c++/OSData.h>
#include <libkern/c++/OSDictionary.h>
#include <libkern/c++/OSIterator.h>
#include <libkern/c++/OSNumber.h>
#include <pexpert/pexpert.h>
#include <string.h>

#include "OriginalUsbXhciABI.h"
#include "usb_hw.h"

using namespace sheng_usb;

extern "C" {
IOReturn armcpu_spi_attach(uint32_t intid, bool edge, void (*handler)(void *), void *context, uint32_t *readback, uint64_t *router);
}

namespace {

constexpr uint64_t kSmmu = 0x15000000, kGicd = 0x17100000;
constexpr uint32_t kSmmuBytes = 0xd0000, kGicdBytes = 0x1000;
constexpr uint32_t kIntId = 165 /* GIC_SPI 133 */, kStreamId = 0x40;
constexpr uint32_t kGicdIsEnabler = 0x100, kGicdIcEnabler = 0x180;
constexpr uint32_t kCbSctlr = 0x0, kCbTcr2 = 0x10, kCbTtbr0 = 0x20, kCbTtbr1 = 0x28, kCbTcr = 0x30, kCbMair0 = 0x38, kCbMair1 = 0x3c,
                   kCbFsr = 0x58, kCbTlbiAsid = 0x610, kCbTlbSync = 0x7f0, kCbTlbStatus = 0x7f4;

volatile uint32_t *mapRegion(uint64_t pa, uint64_t bytes, IOMemoryMap **out)
{
  IOMemoryDescriptor *descriptor = IOMemoryDescriptor::withPhysicalAddress(pa, bytes, kIODirectionInOut);
  IOMemoryMap *map = descriptor ? descriptor->createMappingInTask(kernel_task, 0, kIOMapAnywhere | kIOMapInhibitCache) : nullptr;
  OSSafeReleaseNULL(descriptor);
  *out = map;
  return map ? reinterpret_cast<volatile uint32_t *>(map->getVirtualAddress()) : nullptr;
}

void barrier() { __asm__ volatile("dsb sy" ::: "memory"); }

volatile uint32_t *gGicd;
IOMemoryMap *gGicdMap;

void maskLine(bool masked)
{
  if (!gGicd)
    return;
  const uint32_t word = kIntId / 32 * 4, bit = 1u << (kIntId % 32);
  gGicd[((masked ? kGicdIcEnabler : kGicdIsEnabler) + word) / 4] = bit;
  barrier();
}

}  // namespace

// The controller's provider: a register window and the core's interrupt, in the IOService terms IOFilterInterruptEventSource uses.
class NativeUsbNub : public IOService {
  OSDeclareDefaultStructors(NativeUsbNub)

  IOInterruptAction fAction;
  OSObject *fTarget;
  void *fRefcon;
  bool fEnabled;
  static void primary(void *self);

public:
  IOReturn getInterruptType(int source, int *type) override;
  IOReturn registerInterrupt(int source, OSObject *target, IOInterruptAction handler, void *refcon) override;
  IOReturn unregisterInterrupt(int source) override;
  IOReturn enableInterrupt(int source) override;
  IOReturn disableInterrupt(int source) override;
};

OSDefineMetaClassAndStructors(NativeUsbNub, IOService)

namespace {
bool gLineAttached;
NativeUsbNub *volatile gNub;
volatile uint32_t gInterrupts;
}  // namespace

void NativeUsbNub::primary(void *)
{
  NativeUsbNub *self = gNub;
  __atomic_add_fetch(&gInterrupts, 1, __ATOMIC_RELAXED);
  if (self && self->fAction)
    self->fAction(self->fTarget, self->fRefcon, self, 0);
}

IOReturn NativeUsbNub::getInterruptType(int source, int *type)
{
  if (source != 0 || !type)
    return kIOReturnNoInterrupt;
  *type = kIOInterruptTypeLevel;
  return kIOReturnSuccess;
}

IOReturn NativeUsbNub::registerInterrupt(int source, OSObject *target, IOInterruptAction handler, void *refcon)
{
  if (source != 0 || !handler)
    return kIOReturnNoInterrupt;
  if (fAction)
    return kIOReturnNoResources;
  fTarget = target;
  fRefcon = refcon;
  fAction = handler;
  fEnabled = false;
  gNub = this;
  __asm__ volatile("dmb ish" ::: "memory");
  if (!gLineAttached) {
    const IOReturn result = armcpu_spi_attach(kIntId, false, primary, nullptr, nullptr, nullptr);
    if (result != kIOReturnSuccess) {
      fAction = nullptr;
      return result;
    }
    gLineAttached = true;
  }
  maskLine(true);
  return kIOReturnSuccess;
}

IOReturn NativeUsbNub::unregisterInterrupt(int source)
{
  if (source != 0)
    return kIOReturnNoInterrupt;
  fEnabled = false;
  maskLine(true);
  fAction = nullptr;
  fTarget = nullptr;
  fRefcon = nullptr;
  return kIOReturnSuccess;
}

IOReturn NativeUsbNub::enableInterrupt(int source)
{
  if (source != 0)
    return kIOReturnNoInterrupt;
  fEnabled = true;
  maskLine(false);
  return kIOReturnSuccess;
}

IOReturn NativeUsbNub::disableInterrupt(int source)
{
  if (source != 0)
    return kIOReturnNoInterrupt;
  fEnabled = false;
  maskLine(true);
  return kIOReturnSuccess;
}

// Apple's xHCI driver on this controller: the DWC3 wants 64-bit registers as two 32-bit accesses, high half first on write
// (Linux write-64-hi-lo-quirk), and nothing here tells the family about port power, so the controller stays at its on state.
class NativeUsbXhci final : public AppleUSBXHCI {
  OSDeclareDefaultStructors(NativeUsbXhci)

public:
  uint32_t XHCI_DEFAULT_POWER() override { return 3; }
  uint64_t XHCI_READ64(unsigned long long base, unsigned int offset) override;
  void XHCI_WRITE64(unsigned long long base, unsigned int offset, unsigned long long value) override;
};

OSDefineMetaClassAndStructors(NativeUsbXhci, AppleUSBXHCI)

uint64_t NativeUsbXhci::XHCI_READ64(unsigned long long base, unsigned int offset)
{
  volatile uint32_t *at = reinterpret_cast<volatile uint32_t *>(base + offset);
  const uint64_t low = at[0];
  return low | (uint64_t)at[1] << 32;
}

void NativeUsbXhci::XHCI_WRITE64(unsigned long long base, unsigned int offset, unsigned long long value)
{
  volatile uint32_t *at = reinterpret_cast<volatile uint32_t *>(base + offset);
  at[1] = (uint32_t)(value >> 32);
  barrier();
  at[0] = (uint32_t)value;
  barrier();
}

namespace {

IOBufferMemoryDescriptor *gSmmuTable;
NativeUsbNub *gHostNub;
NativeUsbXhci *gHost;

// A stage 1 context with an identity map of DRAM in 1 GiB blocks, write-back inner shareable: the stream's coherence comes from
// these output attributes, because the shared bypass bank is not cacheable. The window starts at 2 GiB: nothing below is DRAM.
bool smmuIdentity()
{
  IOMemoryMap *map = nullptr;
  volatile uint32_t *smmu = mapRegion(kSmmu, kSmmuBytes, &map);
  if (!smmu)
    return false;
  const uint32_t idr0 = smmu[0x20 / 4], idr1 = smmu[0x24 / 4];
  unsigned groups = idr0 & 255;
  if (groups > 128)
    groups = 128;
  const unsigned banks = idr1 & 255, pages = 1u << (((idr1 >> 28) & 7) + 1);
  if ((idr1 >> 31) || !groups || banks < 2 || banks > 128 || pages > 128 || (pages + banks) * 4096u > kSmmuBytes) {
    IOLog("NativeUsb: unexpected SMMU layout idr0=%08x idr1=%08x\n", idr0, idr1);
    return false;
  }
  uint64_t used[2] = {};
  unsigned slot = 0xffffffffu;
  for (unsigned i = 0; i < groups; ++i) {
    const uint32_t smr = smmu[(0x800 + 4 * i) / 4];
    if (!(smr & 0x80000000u)) {
      if (slot == 0xffffffffu)
        slot = i;
      continue;
    }
    const uint32_t s2cr = smmu[(0xc00 + 4 * i) / 4];
    if (((smr ^ kStreamId) & 0xffff & ~((smr >> 16) & 0x7fff)) == 0) {
      IOLog("NativeUsb: stream already mapped smr=%08x s2cr=%08x\n", smr, s2cr);
      return false;
    }
    if (((s2cr >> 16) & 3) == 0)
      used[(s2cr & 127) / 64] |= 1ull << ((s2cr & 127) % 64);
  }
  if (slot == 0xffffffffu)
    return false;
  // The last bank is the shared bypass bank. Take the highest bank no stream uses and no firmware left enabled.
  unsigned bank = 0xffffffffu;
  for (unsigned b = banks - 1; b-- > 0;) {
    if ((used[b / 64] >> (b % 64)) & 1)
      continue;
    if (smmu[(pages + b) * 1024] & 1u)
      continue;
    bank = b;
    break;
  }
  if (bank == 0xffffffffu)
    return false;
  gSmmuTable = IOBufferMemoryDescriptor::withOptions(static_cast<IOOptionBits>(kIODirectionInOut) | static_cast<IOOptionBits>(kIOMemoryPhysicallyContiguous), 4096, 4096);
  if (!gSmmuTable || gSmmuTable->prepare() != kIOReturnSuccess)
    return false;
  uint64_t *table = static_cast<uint64_t *>(gSmmuTable->getBytesNoCopy());
  bzero(table, 4096);
  for (uint64_t i = 2; i < 64; ++i)
    table[i] = i << 30 | 1ull << 10 | 3ull << 8 | 1ull << 6 | 1ull;  // block, AF, inner shareable, EL0 read/write, attribute 0
  for (uintptr_t at = reinterpret_cast<uintptr_t>(table); at < reinterpret_cast<uintptr_t>(table) + 4096; at += 64)
    __asm__ volatile("dc cvac, %0" ::"r"(at) : "memory");
  barrier();
  const uint64_t tablePa = gSmmuTable->getPhysicalSegment(0, nullptr, 0);
  volatile uint32_t *cb = smmu + (pages + bank) * 1024;
  smmu[(0x1800 + 4 * bank) / 4] = 1;                                  // CBA2R: AArch64
  smmu[(0x1000 + 4 * bank) / 4] = 1u << 16 | 0xfu << 12 | 3u << 8;    // CBAR: stage 1 with stage 2 bypass
  cb[kCbTcr2 / 4] = 2u | 7u << 15;                                    // 40-bit output, upstream sign extension
  cb[kCbTcr / 4] = 28u | 1u << 8 | 1u << 10 | 3u << 12 | 1u << 23;    // walks cacheable inner shareable, 4 KiB, no TTBR1
  cb[kCbTtbr0 / 4] = (uint32_t)tablePa;
  cb[kCbTtbr0 / 4 + 1] = (uint32_t)(tablePa >> 32);
  cb[kCbTtbr1 / 4] = 0;
  cb[kCbTtbr1 / 4 + 1] = 0;
  cb[kCbMair0 / 4] = 0xff;
  cb[kCbMair1 / 4] = 0;
  barrier();
  cb[kCbTlbiAsid / 4] = 0;
  cb[kCbTlbSync / 4] = 0;
  barrier();
  for (unsigned i = 0; i < 1000 && (cb[kCbTlbStatus / 4] & 1u); ++i)
    IODelay(10);
  cb[kCbFsr / 4] = cb[kCbFsr / 4];
  cb[kCbSctlr / 4] = 1u | 1u << 1 | 1u << 2 | 1u << 5 | 1u << 12;     // M, TRE, AFE, CFRE, ASIDPNE
  barrier();
  smmu[(0xc00 + 4 * slot) / 4] = bank;                                // S2CR: translate to this bank, then the match
  barrier();
  smmu[(0x800 + 4 * slot) / 4] = 0x80000000u | kStreamId;
  barrier();
  const bool ok = (cb[kCbSctlr / 4] & 1u) && (smmu[(0xc00 + 4 * slot) / 4] & 255) == bank &&
                  smmu[(0x800 + 4 * slot) / 4] == (0x80000000u | kStreamId);
  IOLog("NativeUsb: SMMU stream %#x slot %u bank %u: %s\n", kStreamId, slot, bank, ok ? "mapped" : "readback failed");
  return ok;
}

bool publishNub(NativeUsbNub **out)
{
  NativeUsbNub *nub = OSTypeAlloc(NativeUsbNub);
  IODeviceMemory *range = IODeviceMemory::withRange(kCore, 0x8000);
  OSArray *ranges = range ? OSArray::withObjects((const OSObject **)&range, 1) : nullptr;
  OSData *zero = OSData::withBytes("\0\0\0\0", 4);
  bool ok = false;
  if (nub && nub->init() && ranges && zero) {
    nub->setDeviceMemory(ranges);
    nub->setName("usb-host0");
    nub->setProperty("bus-number", zero);
    if (nub->attach(IOService::getPlatform())) {
      nub->registerService();
      ok = true;
    }
  }
  OSSafeReleaseNULL(ranges);
  OSSafeReleaseNULL(range);
  OSSafeReleaseNULL(zero);
  if (ok)
    *out = nub;
  else
    OSSafeReleaseNULL(nub);
  return ok;
}

}  // namespace

// Called by native_usb.cpp with the controller in the host role and its PHY running. Returns whether Apple's stack started.
extern "C" bool native_usb_host_start(void)
{
  IOLog("NativeUsb stage 4: starting the macOS USB host stack\n");
  IOMemoryMap *map = nullptr;
  gGicd = mapRegion(kGicd, kGicdBytes, &map);
  gGicdMap = map;
  if (!gGicd) {
    IOLog("NativeUsb: cannot map the GIC distributor\n");
    return false;
  }
  if (!smmuIdentity()) {
    IOLog("NativeUsb: SMMU context failed\n");
    return false;
  }
  // AppleUSBXHCI has no personality of its own; its load brings IOUSBHostFamily and everything that family links.
  const OSReturn loaded = OSKextLoadKextWithIdentifier("com.apple.driver.usb.AppleUSBXHCI");
  IOLog("NativeUsb: load AppleUSBXHCI result %#x\n", loaded);
  if (loaded != kOSReturnSuccess && loaded != kOSKextReturnStartStopError)
    return false;
  // Starts the family's resource services, whose personalities the assembly gated on this resource.
  IOService::publishResource("NativeUsbHost", kOSBooleanTrue);
  NativeUsbNub *nub = nullptr;
  if (!publishNub(&nub)) {
    IOLog("NativeUsb: cannot publish the controller provider\n");
    return false;
  }
  gHostNub = nub;
  NativeUsbXhci *host = OSTypeAlloc(NativeUsbXhci);
  bool started = false;
  if (host && host->init(nullptr) && host->attach(nub)) {
    started = host->start(nub);
    if (!started)
      host->detach(nub);
    else
      gHost = host;
  }
  IOLog("NativeUsb: AppleUSBXHCI %s\n", started ? "started" : "start failed");
  OSSafeReleaseNULL(host);
  return started;
}

extern "C" void native_usb_host_stats(void)
{
  uint32_t count = 0, vendorProduct = 0;
  OSDictionary *match = IOService::serviceMatching("IOUSBHostDevice");
  OSIterator *devices = match ? IOService::getMatchingServices(match) : nullptr;
  OSObject *object;
  while (devices && (object = devices->getNextObject())) {
    ++count;
    IORegistryEntry *entry = OSDynamicCast(IORegistryEntry, object);
    OSNumber *vendor = entry ? OSDynamicCast(OSNumber, entry->getProperty("idVendor")) : nullptr;
    OSNumber *product = entry ? OSDynamicCast(OSNumber, entry->getProperty("idProduct")) : nullptr;
    if (vendor && product)
      vendorProduct = vendor->unsigned32BitValue() << 16 | (product->unsigned32BitValue() & 0xffffu);
  }
  OSSafeReleaseNULL(devices);
  OSSafeReleaseNULL(match);
  IOLog("NativeUsb host: devices=%u last=%04x:%04x interrupts=%u power=%lu\n", count, vendorProduct >> 16, vendorProduct & 0xffffu, gInterrupts,
        gHost ? gHost->getPowerState() : 0ul);
}
