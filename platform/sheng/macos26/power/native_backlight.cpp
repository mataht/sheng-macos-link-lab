// SPDX-License-Identifier: GPL-3.0-or-later
// NativeBacklight: panel brightness of the sheng tablet (ktz8866_i2c.h has the register logic and its sources).
//
// Default off, hardware only (PE_vmm_present == 0). sheng-backlight=<stage>:
//   1  read-only snapshot of the clock votes, branch states and the two engines' identity; no I2C traffic at all
//   2  votes the engines' clocks if needed, prepares the FIFOs and reads both chips' level; the chips are not changed
//   3  also sets sheng-backlight-level=<0..2047> once, clamped to the visible range, and reads it back
//   4  also accepts brightness from macOS through setProperties (IODisplayParameters), clamped the same way
// A level below kMinVisibleLevel is never written, the enable and bias registers are never touched, and any bus error stops
// further writes for this boot. Physical regions (sheng DT, GCC bindings of gcc-sm8550.c as used upstream): GCC 0x00100000
// (0x60000), wrap1 SE1 0x00a84000 (0x4000), I2C hub SE2 0x00988000 (0x4000).
#include <IOKit/IOLib.h>
#include <IOKit/IOMemoryDescriptor.h>
#include <IOKit/IOService.h>
#include <libkern/c++/OSDictionary.h>
#include <libkern/c++/OSNumber.h>
#include <pexpert/pexpert.h>
#include "ktz8866_i2c.h"

namespace {

using namespace sheng_backlight;

constexpr uint64_t kGccPhysical = 0x00100000, kGccBytes = 0x60000;
constexpr uint64_t kSePhysical[2] = {0x00a84000, 0x00988000};
constexpr uint64_t kSeBytes = 0x4000;
constexpr uint32_t kGccVote = 0x52008;
// gcc-sm8550.c: wrap1 core_2x 18, core 19, m_ahb 20, s_ahb 21, s1 23; i2c hub s_ahb 7, core 8, s2 12.
constexpr uint32_t kVoteBits = (1u << 18) | (1u << 19) | (1u << 20) | (1u << 21) | (1u << 23) | (1u << 7) | (1u << 8) | (1u << 12);
constexpr uint32_t kHalt[] = {0x23294, 0x23284, 0x2327c, 0x23280, 0x1813c, 0x23140, 0x23144, 0x1703c};
constexpr unsigned kHaltSpins = 200000;

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
  uint32_t read(uint32_t offset) { return base[offset / 4]; }
  void write(uint32_t offset, uint32_t value)
  {
    base[offset / 4] = value;
    __asm__ volatile("dsb sy" ::: "memory");
  }
};

const char *errorName(Error e)
{
  switch (e) {
  case Error::none: return "ok";
  case Error::watermarkTimeout: case Error::doneTimeout: case Error::readTimeout: return "timeout";
  case Error::watermarkFailed: case Error::doneFailed: case Error::readFailed: return "bus error";
  case Error::readEmpty: return "no data";
  case Error::notI2c: return "not I2C firmware";
  case Error::disabled: return "interface disabled";
  case Error::notClocked: return "serial clock off";
  case Error::noTiming: return "no SCL timing";
  }
  return "?";
}

}  // namespace

class NativeBacklight : public IOService {
  OSDeclareDefaultStructors(NativeBacklight)

  Region fGcc, fSe[2];
  Engine<Region> fEngine[2];
  IOLock *fLock = nullptr;
  uint32_t fLevel = 0;
  bool fBroken = false;

  bool branchesRunning(uint32_t *firstStopped);
  bool ensureClocks();
  void apply(uint32_t requested);
  void publishLevel(uint32_t level);

public:
  bool start(IOService *provider) override;
  IOReturn setProperties(OSObject *properties) override;
};

OSDefineMetaClassAndStructors(NativeBacklight, IOService)

bool NativeBacklight::branchesRunning(uint32_t *firstStopped)
{
  for (uint32_t offset : kHalt) {
    if (!branchRunning(fGcc.read(offset))) {
      *firstStopped = offset;
      return false;
    }
  }
  *firstStopped = 0;
  return true;
}

// Votes only the missing bits, then waits as upstream does.
bool NativeBacklight::ensureClocks()
{
  uint32_t stopped = 0;
  const uint32_t votes = fGcc.read(kGccVote);
  if ((votes & kVoteBits) != kVoteBits) {
    fGcc.write(kGccVote, fGcc.read(kGccVote) | kVoteBits);
    IOLog("NativeBacklight: clock votes %#x -> %#x\n", votes, fGcc.read(kGccVote));
  }
  for (unsigned n = 0; n < kHaltSpins; ++n) {
    if (branchesRunning(&stopped))
      return true;
  }
  IOLog("NativeBacklight: branch %#x did not start\n", stopped);
  return false;
}

// Every level goes through setBoth, which clamps it; a failure stops all further writes this boot.
void NativeBacklight::apply(uint32_t requested)
{
  IOLockLock(fLock);
  if (fBroken) {
    IOLockUnlock(fLock);
    return;
  }
  const uint32_t level = clampLevel(requested);
  const Error error = setBoth(fEngine[0], fEngine[1], level, fLevel);
  if (error == Error::none) {
    fLevel = level;
    uint32_t a = 0, b = 0;
    const Error ra = fEngine[0].readLevel(&a), rb = fEngine[1].readLevel(&b);
    IOLog("NativeBacklight: level %u (requested %u), read back a %u (%s), b %u (%s)\n", level, requested, a, errorName(ra), b,
          errorName(rb));
  } else {
    fBroken = true;
    IOLog("NativeBacklight: write failed (%s), M_IRQ a %#x b %#x; no further writes\n", errorName(error), fEngine[0].lastIrq,
          fEngine[1].lastIrq);
  }
  IOLockUnlock(fLock);
}

void NativeBacklight::publishLevel(uint32_t level)
{
  OSDictionary *brightness = OSDictionary::withCapacity(3), *parameters = OSDictionary::withCapacity(1);
  OSNumber *min = OSNumber::withNumber(static_cast<unsigned long long>(kMinVisibleLevel), 32);
  OSNumber *max = OSNumber::withNumber(static_cast<unsigned long long>(kMaxLevel), 32);
  OSNumber *now = OSNumber::withNumber(static_cast<unsigned long long>(level), 32);
  if (brightness && parameters && min && max && now) {
    brightness->setObject("min", min);
    brightness->setObject("max", max);
    brightness->setObject("value", now);
    parameters->setObject("brightness", brightness);
    setProperty("IODisplayParameters", parameters);
  }
  OSSafeReleaseNULL(min);
  OSSafeReleaseNULL(max);
  OSSafeReleaseNULL(now);
  OSSafeReleaseNULL(brightness);
  OSSafeReleaseNULL(parameters);
}

IOReturn NativeBacklight::setProperties(OSObject *properties)
{
  OSDictionary *dictionary = OSDynamicCast(OSDictionary, properties);
  if (!dictionary)
    return kIOReturnBadArgument;
  OSNumber *brightness = OSDynamicCast(OSNumber, dictionary->getObject("brightness"));
  if (!brightness)
    return kIOReturnSuccess;
  const uint32_t requested = brightness->unsigned32BitValue();
  apply(requested);
  publishLevel(clampLevel(requested));
  return kIOReturnSuccess;
}

bool NativeBacklight::start(IOService *provider)
{
  uint32_t stage = 0;
  if (!PE_parse_boot_argn("sheng-backlight", &stage, sizeof(stage)) || stage < 1 || stage > 4)
    return false;
  if (PE_vmm_present != 0) {
    IOLog("NativeBacklight: the panel backlight is not driven in a VM\n");
    return false;
  }
  if (!IOService::start(provider))
    return false;
  fLock = IOLockAlloc();
  if (!fLock || !fGcc.open(kGccPhysical, kGccBytes) || !fSe[0].open(kSePhysical[0], kSeBytes) || !fSe[1].open(kSePhysical[1], kSeBytes)) {
    IOLog("NativeBacklight: cannot map its registers\n");
    return false;
  }
  fEngine[0].reg = &fSe[0];
  fEngine[1].reg = &fSe[1];

  // A hardware-gated branch reads off while idle, so its status proves nothing: the engines count as clocked only when all
  // their votes are already set. (Touch stage 1 observed these bits set at boot, vote register 0x40bc1380.)
  uint32_t stopped = 0;
  const uint32_t votes = fGcc.read(kGccVote);
  bool running = (votes & kVoteBits) == kVoteBits && branchesRunning(&stopped);
  IOLog("NativeBacklight: stage %u, votes %#x, clocks %s (stopped branch %#x)\n", stage, votes,
        running ? "running" : "not all voted and running", stopped);
  if (stage >= 2 && !running && !ensureClocks())
    return false;
  if (stage == 1 && !running) {
    IOLog("NativeBacklight: engines not read, a stopped clock could fault\n");
    return false;
  }

  uint32_t summary[2] = {};
  const Error identified[2] = {fEngine[0].identify(&summary[0]), fEngine[1].identify(&summary[1])};
  IOLog("NativeBacklight: engine a proto|clk %#x (%s), engine b %#x (%s)\n", summary[0], errorName(identified[0]), summary[1],
        errorName(identified[1]));
  if (stage == 1) {
    registerService();
    return true;
  }
  if (identified[0] != Error::none || identified[1] != Error::none) {
    IOLog("NativeBacklight: an engine is not usable, stopping\n");
    return false;
  }

  fEngine[0].prepare();
  fEngine[1].prepare();
  uint32_t levels[2] = {};
  const Error reads[2] = {fEngine[0].readLevel(&levels[0]), fEngine[1].readLevel(&levels[1])};
  IOLog("NativeBacklight: chip a level %u (%s), chip b level %u (%s)\n", levels[0], errorName(reads[0]), levels[1],
        errorName(reads[1]));
  if (reads[0] != Error::none || reads[1] != Error::none) {
    IOLog("NativeBacklight: a chip did not answer (M_IRQ a %#x b %#x), stopping\n", fEngine[0].lastIrq, fEngine[1].lastIrq);
    return false;
  }
  fLevel = levels[0];

  if (stage >= 3) {
    uint32_t requested = 0;
    if (PE_parse_boot_argn("sheng-backlight-level", &requested, sizeof(requested)))
      apply(requested);
    else
      IOLog("NativeBacklight: no sheng-backlight-level given, level left at %u\n", fLevel);
  }
  if (stage == 4) {
    publishLevel(fLevel);
    IOLog("NativeBacklight: accepting brightness from macOS, range %u..%u\n", kMinVisibleLevel, kMaxLevel);
  }
  registerService();
  return true;
}
