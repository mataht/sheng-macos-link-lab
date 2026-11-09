// SPDX-License-Identifier: GPL-3.0-or-later
// NativeRtc: start the XNU calendar from the PMK8550 RTC on the sheng tablet instead of 1970.
//
// Default off: sheng-rtc=1 only reads the RTC and logs it; sheng-rtc=2 also sets the calendar. Hardware only (PE_vmm_present == 0).
// See native_rtc.h for the register sources; only reads are issued.
//
// bsd_init calls IOKitInitializeTime, which waits up to 30 s for the IORTC resource and then asks the platform expert for the
// time. On the tablet (boot 05) this driver started only after that, so in mode 2 the platform expert publishes IORTC itself to
// skip the wait, and this driver moves the calendar forward when it runs. It also stores the time for
// ArmDTPlatform::getGMTTimeOfDay, which covers the case that it runs first. The calendar only ever moves forward here. Data, not
// a function pointer, crosses from this module to the kernel, and nothing is written back: PESetUTCTimeOfDay stays the platform
// expert's no-op.
//
// Boot arguments, both optional and 32-bit: sheng-rtc-offset=<wall clock minus raw counter>, sheng-rtc-floor=<Unix seconds>.
// Physical regions (sheng DT /soc/qcom,spmi@c42d000, read-only use): core 0x0c400000 (0x3000), cnfg 0x0c42d000 (0x4000),
// obsrvr 0x0c440000 (the EE 0 window, 0x8000), the same as NativeButtonKeys; each peripheral has its own observer channel.
#include <IOKit/IOLib.h>
#include <IOKit/IOMemoryDescriptor.h>
#include <IOKit/IOService.h>
#include <kern/clock.h>
#include <kern/thread.h>
#include <pexpert/pexpert.h>
#include "native_rtc.h"

extern "C" {
extern volatile long sheng_platform_rtc_seconds;
extern volatile uint64_t sheng_platform_rtc_stamp;
void clock_set_calendar_microtime(clock_sec_t secs, clock_usec_t microsecs);
}

namespace {

constexpr uint64_t kCorePhysical = 0x0c400000, kCoreBytes = 0x3000;
constexpr uint64_t kCnfgPhysical = 0x0c42d000, kCnfgBytes = 0x4000;
constexpr uint64_t kObsPhysical = 0x0c440000, kObsBytes = 0x8000;
constexpr unsigned kFollowUpMs = 5000;

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
  case sheng_keys::Status::dropped: return "unstable";
  }
  return "?";
}

}  // namespace

class NativeRtc : public IOService {
  OSDeclareDefaultStructors(NativeRtc)

  Io fIo;
  sheng_keys::Arbiter fArbiter;
  int fApid = -1;
  uint32_t fFirstSeconds = 0;

  static void followUpThread(void *self, wait_result_t);

public:
  bool start(IOService *provider) override;
};

OSDefineMetaClassAndStructors(NativeRtc, IOService)

void NativeRtc::followUpThread(void *self, wait_result_t)
{
  NativeRtc *rtc = static_cast<NativeRtc *>(self);
  IOSleep(kFollowUpMs);
  const sheng_rtc::Reading later = sheng_rtc::read(rtc->fArbiter, rtc->fApid);
  IOLog("NativeRtc: after %u ms raw %u s (%s), advanced %d s\n", kFollowUpMs, later.seconds, statusName(later.status),
        static_cast<int>(later.seconds - rtc->fFirstSeconds));
}

bool NativeRtc::start(IOService *provider)
{
  uint32_t mode = 0;
  if (!PE_parse_boot_argn("sheng-rtc", &mode, sizeof(mode)) || mode < 1 || mode > 2)
    return false;
  if (PE_vmm_present != 0) {
    IOLog("NativeRtc: the PMIC RTC is not read in a VM\n");
    return false;
  }
  if (!IOService::start(provider))
    return false;

  sheng_rtc::Policy policy;
  policy.haveOffset = PE_parse_boot_argn("sheng-rtc-offset", &policy.offset, sizeof(policy.offset));
  PE_parse_boot_argn("sheng-rtc-floor", &policy.floor, sizeof(policy.floor));

  sheng_rtc::Reading first;
  sheng_keys::Status probed = sheng_keys::Status::unsupported;
  if (fIo.coreRegion.open(kCorePhysical, kCoreBytes) && fIo.cnfgRegion.open(kCnfgPhysical, kCnfgBytes) &&
      fIo.obsRegion.open(kObsPhysical, kObsBytes)) {
    fArbiter.io = &fIo;
    fArbiter.coreBytes = kCoreBytes;
    fArbiter.cnfgBytes = kCnfgBytes;
    fArbiter.obsBytes = kObsBytes;
    fArbiter.ee = 0;
    probed = sheng_keys::probe(fArbiter);
    if (probed == sheng_keys::Status::ok) {
      fApid = sheng_rtc::channel(fArbiter);
      first = sheng_rtc::read(fArbiter, fApid);
    }
  } else {
    IOLog("NativeRtc: cannot map the SPMI arbiter\n");
  }
  IOLog("NativeRtc: mode %u, arbiter version %#x (%s), %u APIDs, RTC APID %d\n", mode, fArbiter.version, statusName(probed),
        fArbiter.apids, fApid);
  IOLog("NativeRtc: raw %u s, enabled %u, read %s, %u attempts\n", first.seconds, first.enabled, statusName(first.status),
        first.attempts);

  fFirstSeconds = first.seconds;
  const sheng_rtc::Time wall = sheng_rtc::wallClock(first, policy);
  if (mode == 2) {
    if (wall.source != sheng_rtc::Source::none) {
      uint64_t now = 0;
      clock_get_uptime(&now);
      sheng_platform_rtc_stamp = now;
      __atomic_store_n(&sheng_platform_rtc_seconds, static_cast<long>(wall.seconds), __ATOMIC_RELEASE);
      clock_sec_t current = 0;
      clock_usec_t microseconds = 0;
      clock_get_calendar_microtime(&current, &microseconds);
      const bool forward = static_cast<int64_t>(current) < wall.seconds;
      if (forward)
        clock_set_calendar_microtime(static_cast<clock_sec_t>(wall.seconds), 0);
      IOLog("NativeRtc: calendar source %s, %ld s, %s (was %ld s)\n", sheng_rtc::sourceName(wall.source),
            static_cast<long>(wall.seconds), forward ? "moved forward" : "left alone", static_cast<long>(current));
    } else {
      IOLog("NativeRtc: no time source, calendar left alone\n");
    }
  } else {
    IOLog("NativeRtc: read-only, calendar untouched (would use %s, %ld s)\n", sheng_rtc::sourceName(wall.source),
          static_cast<long>(wall.seconds));
  }

  if (first.status == sheng_keys::Status::ok) {
    retain();
    thread_t thread;
    if (kernel_thread_start(followUpThread, this, &thread) == KERN_SUCCESS)
      thread_deallocate(thread);
    else
      release();
  }
  registerService();
  return true;
}
