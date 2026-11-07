// SPDX-License-Identifier: GPL-3.0-or-later
// NativeButtonKeys: the sheng power and volume keys as a boot keyboard, so Recovery can be driven without touch.
// Volume up = Up arrow, volume down = Down arrow, power = Return, both volume keys = Tab.
//
// Default off: native-button-keys=1, on hardware only (PE_vmm_present == 0). A kernel thread polls the keys every 20 ms through
// the SPMI PMIC arbiter's observer (read) channel; see spmi_keys.h for the register sources. Only reads are issued: the arbiter
// write channels, PMIC writes and interrupts are not used. If the arbiter is not the expected v7 or the PON channel cannot be
// found, the driver declines before starting the HID device. A long press of power (about 10 s) still resets the PMIC.
//
// Physical regions (sheng DT /soc/qcom,spmi@c42d000, read-only use): core 0x0c400000 (0x3000), cnfg 0x0c42d000 (0x4000),
// obsrvr 0x0c440000 (the EE 0 window, 0x8000).
#include <IOKit/IOBufferMemoryDescriptor.h>
#include <IOKit/IOLib.h>
#include <IOKit/IOMemoryDescriptor.h>
#include <IOKit/IOService.h>
#include <IOKit/hid/IOHIDDevice.h>
#include <kern/thread.h>
#include <pexpert/pexpert.h>
#include "spmi_keys.h"

static_assert(sizeof(IOHIDDevice) == 208, "paired 25F71 IOHIDDevice size");

namespace {

constexpr uint64_t kCorePhysical = 0x0c400000, kCoreBytes = 0x3000;
constexpr uint64_t kCnfgPhysical = 0x0c42d000, kCnfgBytes = 0x4000;
constexpr uint64_t kObsPhysical = 0x0c440000, kObsBytes = 0x8000;
constexpr unsigned kPollMs = 20, kMaxConsecutiveFailures = 50;

const uint8_t keyboardDescriptor[] = {
  0x05, 0x01, 0x09, 0x06, 0xa1, 0x01,
  0x75, 0x01, 0x95, 0x08, 0x05, 0x07, 0x19, 0xe0, 0x29, 0xe7, 0x15, 0x00, 0x25, 0x01, 0x81, 0x02,
  0x95, 0x01, 0x75, 0x08, 0x81, 0x01,
  0x95, 0x06, 0x75, 0x08, 0x15, 0x00, 0x26, 0xff, 0x00, 0x05, 0x07, 0x19, 0x00, 0x2a, 0xff, 0x00, 0x81, 0x00,
  0xc0,
};

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

struct Io final : sheng_keys::Mmio {
  Region coreRegion, cnfgRegion, obsRegion;
  uint32_t core(uint32_t o) override { return coreRegion.base[o / 4]; }
  uint32_t cnfg(uint32_t o) override { return cnfgRegion.base[o / 4]; }
  uint32_t readObs(uint32_t o) override { return obsRegion.base[o / 4]; }
  void writeObs(uint32_t o, uint32_t v) override
  {
    __asm__ volatile("dsb sy" ::: "memory");
    obsRegion.base[o / 4] = v;
    __asm__ volatile("dsb sy" ::: "memory");
  }
  void delayMicroseconds(unsigned us) override { IODelay(us); }
};

const char *statusName(sheng_keys::Status s)
{
  switch (s) {
  case sheng_keys::Status::ok: return "ok";
  case sheng_keys::Status::unsupported: return "unsupported";
  case sheng_keys::Status::notFound: return "not found";
  case sheng_keys::Status::timeout: return "timeout";
  case sheng_keys::Status::failure: return "failure";
  case sheng_keys::Status::denied: return "denied";
  case sheng_keys::Status::dropped: return "dropped";
  }
  return "?";
}

}  // namespace

class NativeButtonHID : public IOHIDDevice {
  OSDeclareDefaultStructors(NativeButtonHID)

public:
  IOReturn newReportDescriptor(IOMemoryDescriptor **descriptor) const override
  {
    *descriptor = IOBufferMemoryDescriptor::withBytes(keyboardDescriptor, sizeof(keyboardDescriptor), kIODirectionNone);
    return *descriptor ? kIOReturnSuccess : kIOReturnNoMemory;
  }
  OSString *newTransportString() const override { return OSString::withCString("SPMI"); }
  OSString *newManufacturerString() const override { return OSString::withCString("Xiaomi"); }
  OSString *newProductString() const override { return OSString::withCString("Sheng Hardware Keys"); }
  OSNumber *newVendorIDNumber() const override { return OSNumber::withNumber(0x1209, 32); }
  OSNumber *newProductIDNumber() const override { return OSNumber::withNumber(0x5348, 32); }
  OSNumber *newLocationIDNumber() const override { return OSNumber::withNumber(0x53504d49, 32); }
  OSNumber *newPrimaryUsagePageNumber() const override { return OSNumber::withNumber(kHIDPage_GenericDesktop, 32); }
  OSNumber *newPrimaryUsageNumber() const override { return OSNumber::withNumber(kHIDUsage_GD_Keyboard, 32); }
};

OSDefineMetaClassAndStructors(NativeButtonHID, IOHIDDevice)

class NativeButtonKeys : public IOService {
  OSDeclareDefaultStructors(NativeButtonKeys)

  Io fIo;
  sheng_keys::Arbiter fArbiter;
  sheng_keys::Channels fChannels;
  sheng_keys::Debounce fDebounce;
  NativeButtonHID *fHid = nullptr;
  unsigned fReports = 0;

  static void pollThread(void *self, wait_result_t);
  void post(const sheng_keys::Keys &keys);

public:
  bool start(IOService *provider) override;
};

OSDefineMetaClassAndStructors(NativeButtonKeys, IOService)

bool NativeButtonKeys::start(IOService *provider)
{
  unsigned enabled = 0;
  if (!PE_parse_boot_argn("native-button-keys", &enabled, sizeof(enabled)) || enabled != 1)
    return false;
  if (PE_vmm_present != 0) {
    IOLog("NativeButtonKeys: hardware keys are not polled in a VM\n");
    return false;
  }
  if (!IOService::start(provider))
    return false;
  if (!fIo.coreRegion.open(kCorePhysical, kCoreBytes) || !fIo.cnfgRegion.open(kCnfgPhysical, kCnfgBytes) ||
      !fIo.obsRegion.open(kObsPhysical, kObsBytes)) {
    IOLog("NativeButtonKeys: cannot map the SPMI arbiter\n");
    fIo.coreRegion.close(), fIo.cnfgRegion.close(), fIo.obsRegion.close();
    return false;
  }
  fArbiter.io = &fIo;
  fArbiter.coreBytes = kCoreBytes;
  fArbiter.cnfgBytes = kCnfgBytes;
  fArbiter.obsBytes = kObsBytes;
  fArbiter.ee = 0;
  const sheng_keys::Status probed = sheng_keys::probe(fArbiter);
  fChannels = probed == sheng_keys::Status::ok ? sheng_keys::resolve(fArbiter) : sheng_keys::Channels();
  IOLog("NativeButtonKeys: SPMI arbiter version %#x (%s), %u APIDs, PON APID %d, vol-up GPIO APID %d\n", fArbiter.version,
        statusName(probed), fArbiter.apids, fChannels.pon, fChannels.gpio);
  if (probed != sheng_keys::Status::ok || fChannels.pon < 0) {
    fIo.coreRegion.close(), fIo.cnfgRegion.close(), fIo.obsRegion.close();
    return false;
  }
  sheng_keys::Keys first;
  sheng_keys::Status ponStatus, gpioStatus;
  const bool sampled = sheng_keys::sample(fArbiter, fChannels, &first, &ponStatus, &gpioStatus);
  IOLog("NativeButtonKeys: first sample PON %s, GPIO %s: power %u, volume down %u, volume up %u\n", statusName(ponStatus),
        statusName(gpioStatus), first.power, first.volumeDown, first.volumeUp);
  if (!sampled) {
    fIo.coreRegion.close(), fIo.cnfgRegion.close(), fIo.obsRegion.close();
    return false;
  }
  fHid = OSTypeAlloc(NativeButtonHID);
  if (!fHid || !fHid->init(nullptr) || !fHid->attach(this)) {
    OSSafeReleaseNULL(fHid);
  } else if (!fHid->start(this)) {
    fHid->detach(this);
    OSSafeReleaseNULL(fHid);
  }
  if (!fHid) {
    IOLog("NativeButtonKeys: cannot start the HID device\n");
    return false;
  }
  // The companion cannot unload. Retain the provider for the lifetime of its polling thread.
  retain();
  thread_t thread;
  if (kernel_thread_start(pollThread, this, &thread) != KERN_SUCCESS) {
    release();
    IOLog("NativeButtonKeys: cannot start the polling thread\n");
    return false;
  }
  thread_deallocate(thread);
  registerService();
  IOLog("NativeButtonKeys: started (volume up = Up, volume down = Down, power = Return, both volume = Tab)\n");
  return true;
}

void NativeButtonKeys::pollThread(void *self, wait_result_t)
{
  NativeButtonKeys *keys = static_cast<NativeButtonKeys *>(self);
  unsigned failures = 0;
  for (;;) {
    IOSleep(kPollMs);
    sheng_keys::Keys now;
    sheng_keys::Status ponStatus, gpioStatus;
    if (!sheng_keys::sample(keys->fArbiter, keys->fChannels, &now, &ponStatus, &gpioStatus)) {
      if (++failures == kMaxConsecutiveFailures) {
        IOLog("NativeButtonKeys: %u consecutive PON read failures (%s); polling stopped\n", failures, statusName(ponStatus));
        sheng_keys::Keys released;
        keys->post(released);
        return;
      }
      continue;
    }
    failures = 0;
    if (keys->fDebounce.update(now))
      keys->post(now);
  }
}

void NativeButtonKeys::post(const sheng_keys::Keys &keys)
{
  uint8_t bytes[8];
  sheng_keys::report(keys, bytes);
  if (++fReports <= 40 || fReports % 200 == 0)
    IOLog("NativeButtonKeys: report %u: power %u volume down %u volume up %u -> %02x %02x %02x\n", fReports, keys.power,
          keys.volumeDown, keys.volumeUp, bytes[2], bytes[3], bytes[4]);
  IOBufferMemoryDescriptor *report = IOBufferMemoryDescriptor::withBytes(bytes, sizeof(bytes), kIODirectionNone);
  if (report) {
    fHid->handleReport(report);
    report->release();
  }
}
