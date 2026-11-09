// SPDX-License-Identifier: GPL-3.0-or-later
// NativeKeyboard: macOS 26 front end for the sheng keyboard cover (Nanosic WN8030 controller on I2C9: QUP2 SE1 at 0x884000, client 0x4c).
// The GENI I2C core, the WN8030 codec and the RPMh request builder are upstream UniXNU-sheng files, imported unchanged in keyboard/. Upstream
// runs this cover on iPadOS from interrupts it shares; here one kernel thread polls instead, so no interrupt line is taken from NativeTouch.
//
// Default off: sheng-kbd=<1..5>, hardware only (PE_vmm_present == 0). The stages add up, and the first failure stops the sequence:
//   1  read-only snapshot: L6B, L12B, L13B through the SPMI observer, the cover's pins, the QUP2 clock branches. Touches no engine register.
//   2  QUP2 clock votes, I2C pin function, then the engine identity (protocol, FIFO state). No bus traffic.
//   3  reset and sleep lines, engine setup, controller power-on, boot ROM id and state: the first I2C traffic.
//   4  the controller RAM image from /arm-io/native-keyboard (local package only), boot, then serving: frames, attach state, keep-awake.
//   5  a boot keyboard HID device fed from the cover's key reports.
// Options: sheng-kbd-khz=<100|400|1000> (default 1000, as upstream and Android), sheng-kbd-rails=1 votes L12B 1.8 V and L13B 3296 mV through
// RPMh (default off: stage 1 shows whether they are on), sheng-kbd-poll-read=<10..1000> also reads a frame every that many ms without an edge.
//
// Authentication requests of the cover stay unanswered; they are counted. Stage 4 prints the first frames as hex, so type only test keys in it.
// Stage 5 never prints a frame or a report: what is typed there can be a password.
#include <IOKit/IOBufferMemoryDescriptor.h>
#include <IOKit/IODeviceMemory.h>
#include <IOKit/IODeviceTreeSupport.h>
#include <IOKit/IOLib.h>
#include <IOKit/IOMemoryDescriptor.h>
#include <IOKit/IOService.h>
#include <IOKit/hid/IOHIDDevice.h>
#include <kern/clock.h>
#include <kern/thread.h>
#include <libkern/c++/OSData.h>
#include <libkern/libkern.h>
#include <pexpert/pexpert.h>
#include <string.h>
#include "spmi_keys.h"
extern "C" {
#include "KeyboardNames.h"
#include "GeniI2c.h"
#include "QcomRpmh.h"
#include "TouchBoard.h"
#include "Wn8030.h"
}

static_assert(sizeof(IOHIDDevice) == 208, "paired 25F71 IOHIDDevice size");

namespace {

#ifndef NATIVE_KEYBOARD_HOST_MMIO
inline void dsb() { __asm__ volatile("dsb sy" ::: "memory"); }
inline uint32_t rd32(uintptr_t base, uint32_t reg) { return *reinterpret_cast<volatile uint32_t *>(base + reg); }
inline void wr32(uintptr_t base, uint32_t reg, uint32_t value)
{
  *reinterpret_cast<volatile uint32_t *>(base + reg) = value;
  dsb();
}
#endif

constexpr uint64_t kGcc = 0x00100000, kTlmm = 0x0f100000, kSe = 0x00884000, kWrapper = 0x008c0000, kRsc = 0x17a20000, kCmdDb = 0x81c60000;
constexpr uint64_t kSpmiCore = 0x0c400000, kSpmiCoreBytes = 0x3000, kSpmiCnfg = 0x0c42d000, kSpmiCnfgBytes = 0x4000, kSpmiObs = 0x0c440000,
                   kSpmiObsBytes = 0x8000;
constexpr uint32_t kCmdDbBytes = 0x20000, kRscTcs = 0xd00, kTcs = 2;
// gcc-sm8550.c: QUP2 branch votes in 0x52010 (bit 0 wrap2 core, 1 S AHB, 2 M AHB, 3 core 2x, 5 SE1), their halt registers, the SE1 RCG.
constexpr uint32_t kVote = 0x52010, kVoteBits = (1u << 0) | (1u << 1) | (1u << 2) | (1u << 3) | (1u << 5);
constexpr uint32_t kHalts[] = {0x233d4, 0x233d0, 0x233cc, 0x23004, 0x1e13c};
constexpr unsigned kHaltCount = sizeof(kHalts) / sizeof(*kHalts);
constexpr uint32_t kRcg = 0x1e148;
// TLMM per pin (pinctrl-sm8550.c): CTL bits 1:0 pull (1 down, 3 up), 5:2 function, 8:6 drive, 9 output enable; IO bit 0 in, bit 1 out;
// INTR_CFG bit 0 enable, 1 polarity, 3:2 detection (1 rising), 4 raw status, 7:5 target (3 the application processor); INTR_STATUS bit 0.
constexpr uint32_t kCtl = 0x0, kIo = 0x4, kIntrCfg = 0x8, kIntrStatus = 0xc;
enum PinIndex { PinStatus, PinSleep, PinReset, PinSda, PinScl, PinIrq, PinLid, PinTablet, PinCount };
constexpr unsigned kPins[PinCount] = {56, 57, 58, 60, 61, 62, 171, 172};

constexpr unsigned kStartDelayMs = 3000, kBusPollUs = 20, kPollAttachedMs = 2, kPollIdleMs = 20, kKeepAliveMs = 12000;
constexpr unsigned kMaxFailures = 50, kReportEveryMs = 5000, kReportForMs = 120000, kFramesLogged = 24;
constexpr uint8_t kReleased[8] = {};

const uint8_t keyboardDescriptor[] = {
  0x05, 0x01, 0x09, 0x06, 0xa1, 0x01,
  0x75, 0x01, 0x95, 0x08, 0x05, 0x07, 0x19, 0xe0, 0x29, 0xe7, 0x15, 0x00, 0x25, 0x01, 0x81, 0x02,
  0x95, 0x01, 0x75, 0x08, 0x81, 0x01,
  0x95, 0x06, 0x75, 0x08, 0x15, 0x00, 0x26, 0xff, 0x00, 0x05, 0x07, 0x19, 0x00, 0x2a, 0xff, 0x00, 0x81, 0x00,
  0xc0,
};

struct Region {
  IOMemoryMap *map = nullptr;
  uintptr_t va = 0;
  bool open(uint64_t physical, uint64_t bytes)
  {
    if (va)
      return true;
    IODeviceMemory *memory = IODeviceMemory::withRange(physical, bytes);
    if (!memory)
      return false;
    map = memory->map(kIOMapAnywhere | kIOMapInhibitCache);
    memory->release();
    va = map ? static_cast<uintptr_t>(map->getVirtualAddress()) : 0;
    return va != 0;
  }
};

struct SpmiIo final : sheng_keys::Mmio {
  Region coreRegion, cnfgRegion, obsRegion;
  uint32_t core(uint32_t o) override { return rd32(coreRegion.va, o); }
  uint32_t cnfg(uint32_t o) override { return rd32(cnfgRegion.va, o); }
  uint32_t readObs(uint32_t o) override { return rd32(obsRegion.va, o); }
  void writeObs(uint32_t o, uint32_t v) override
  {
    dsb();
    wr32(obsRegion.va, o, v);
  }
  void delayMicroseconds(unsigned us) override { IODelay(us); }
};

uint32_t ioRead(void *context, uint32_t reg) { return rd32(reinterpret_cast<uintptr_t>(context), reg); }
void ioWrite(void *context, uint32_t reg, uint32_t value) { wr32(reinterpret_cast<uintptr_t>(context), reg, value); }
uint64_t nowMs()
{
  uint64_t ns = 0;
  absolutetime_to_nanoseconds(mach_absolute_time(), &ns);
  return ns / 1000000;
}

}  // namespace

class NativeKeyboardHID : public IOHIDDevice {
  OSDeclareDefaultStructors(NativeKeyboardHID)

public:
  IOReturn newReportDescriptor(IOMemoryDescriptor **descriptor) const override
  {
    *descriptor = IOBufferMemoryDescriptor::withBytes(keyboardDescriptor, sizeof(keyboardDescriptor), kIODirectionNone);
    return *descriptor ? kIOReturnSuccess : kIOReturnNoMemory;
  }
  OSString *newTransportString() const override { return OSString::withCString("I2C"); }
  OSString *newManufacturerString() const override { return OSString::withCString("Xiaomi"); }
  OSString *newProductString() const override { return OSString::withCString("Sheng Keyboard Cover"); }
  OSNumber *newVendorIDNumber() const override { return OSNumber::withNumber(0x1209, 32); }
  OSNumber *newProductIDNumber() const override { return OSNumber::withNumber(0x534b, 32); }
  OSNumber *newLocationIDNumber() const override { return OSNumber::withNumber(0x574e3830, 32); }
  OSNumber *newPrimaryUsagePageNumber() const override { return OSNumber::withNumber(kHIDPage_GenericDesktop, 32); }
  OSNumber *newPrimaryUsageNumber() const override { return OSNumber::withNumber(kHIDUsage_GD_Keyboard, 32); }
};

OSDefineMetaClassAndStructors(NativeKeyboardHID, IOHIDDevice)

class NativeKeyboard : public IOService {
  OSDeclareDefaultStructors(NativeKeyboard)

  uint32_t fStage = 0, fKhz = 1000, fPollReadMs = 0;
  bool fRails = false;
  uint32_t fDone = 0, fStep = 0;
  int32_t fError = 0;
  uint64_t fStartMs = 0;
  NativeKeyboardHID *fHid = nullptr;
  const uint8_t *fFirmware = nullptr;
  uint32_t fFirmwareBytes = 0, fCodeBytes = 0;
  SpmiIo fSpmi;
  sheng_keys::Arbiter fArbiter;
  Region fGccLow, fGcc1e, fGcc23, fGccVote, fSe, fWrapper, fPin[PinCount];
  GeniI2c fBus = {};
  uint8_t fFrame[WN8030_FRAME_BYTES] = {};
  uint8_t fCommand[1 + WN8030_DATA_COMMAND_BYTES] = {};
  int fRomId = -1, fRomState = -1, fRomHeader = -1, fRomVerified = -1;
  uint32_t fImageMs = 0, fEdges = 0, fFrames = 0, fInvalid = 0, fKeyReports = 0, fConsumer = 0, fTouchpad = 0, fAuth = 0, fOther = 0;
  uint32_t fFailures = 0, fKeepAlives = 0, fAttaches = 0;
  bool fAttached = false;

  static void threadEntry(void *self, wait_result_t);
  void run();
  void line(const char *text);
  bool fail(uint32_t step, int32_t detail);
  void loadFirmware();
  bool snapshot();
  void snapshotRails();
  void snapshotPins();
  void snapshotClocks();
  bool enableClocks();
  bool readEngine();
  bool setupPins();
  bool setupRails();
  bool setupBus();
  bool powerOn();
  bool loadImage();
  void serve();
  void printServing();
  bool handleFrame();
  void logFrame();
  void sendReport(const uint8_t *bytes);
  template <typename Start> int transfer(Start start, uint32_t bytes);
  bool romWrite(uint8_t value);
  int romRead();
  int romState();
  bool writeBuffer(const uint8_t *bytes, uint32_t length);
  bool readFrame();
  bool control(uint8_t command, uint8_t value);

public:
  bool start(IOService *provider) override;
};

OSDefineMetaClassAndStructors(NativeKeyboard, IOService)

bool NativeKeyboard::start(IOService *provider)
{
  if (!PE_parse_boot_argn("sheng-kbd", &fStage, sizeof(fStage)) || fStage < 1 || fStage > 5)
    return false;
  if (PE_vmm_present != 0) {
    IOLog("NativeKeyboard: the keyboard cover is not driven in a VM\n");
    return false;
  }
  if (!IOService::start(provider))
    return false;
  uint32_t value = 0;
  if (PE_parse_boot_argn("sheng-kbd-khz", &value, sizeof(value)) && (value == 100 || value == 400 || value == 1000))
    fKhz = value;
  value = 0;
  fRails = PE_parse_boot_argn("sheng-kbd-rails", &value, sizeof(value)) && value == 1;
  value = 0;
  if (PE_parse_boot_argn("sheng-kbd-poll-read", &value, sizeof(value)) && value >= 10 && value <= 1000)
    fPollReadMs = value;
  if (fStage >= 4)
    loadFirmware();
  if (fStage >= 5) {
    fHid = OSTypeAlloc(NativeKeyboardHID);
    if (!fHid || !fHid->init(nullptr) || !fHid->attach(this)) {
      OSSafeReleaseNULL(fHid);
    } else if (!fHid->start(this)) {
      fHid->detach(this);
      OSSafeReleaseNULL(fHid);
    }
    if (!fHid) {
      IOLog("NativeKeyboard: cannot start the HID device\n");
      return false;
    }
  }
  retain();   // the companion cannot unload; the thread uses this object for the life of the system
  thread_t thread = nullptr;
  if (kernel_thread_start(threadEntry, this, &thread) != KERN_SUCCESS) {
    release();
    IOLog("NativeKeyboard: cannot start the bring-up thread\n");
    return false;
  }
  thread_deallocate(thread);
  registerService();
  IOLog("NativeKeyboard: stage %u started, %u kHz, rails vote %s\n", fStage, fKhz, fRails ? "on" : "off");
  return true;
}

void NativeKeyboard::loadFirmware()
{
  IORegistryEntry *node = IORegistryEntry::fromPath("/arm-io/native-keyboard", gIODTPlane);
  OSData *image = node ? OSDynamicCast(OSData, node->getProperty("firmware")) : nullptr;
  uint32_t code = 0;
  int checked = -100;
  if (image) {
    checked = wn8030_image_check(static_cast<const uint8_t *>(image->getBytesNoCopy()), image->getLength(), &code);
    if (!checked) {
      uint8_t *copy = static_cast<uint8_t *>(IOMalloc(image->getLength()));
      if (copy) {
        memcpy(copy, image->getBytesNoCopy(), image->getLength());
        fFirmware = copy;
        fFirmwareBytes = image->getLength();
        fCodeBytes = code;
      }
    }
  }
  IOLog("NativeKeyboard: controller image %s (check %d, %u bytes)\n", fFirmware ? "accepted" : "absent or rejected", checked, fFirmwareBytes);
  if (node)
    node->release();
}

void NativeKeyboard::threadEntry(void *self, wait_result_t)
{
  static_cast<NativeKeyboard *>(self)->run();
}

void NativeKeyboard::line(const char *text)
{
  IOLog("NativeKeyboard t%us: %s\n", static_cast<unsigned>((nowMs() - fStartMs) / 1000), text);
}

bool NativeKeyboard::fail(uint32_t step, int32_t detail)
{
  if (!fError) {
    fStep = step;
    fError = detail ? detail : -1;
  }
  char text[120];
  snprintf(text, sizeof(text), "KBD fail s%u/%u step%u err%d bus e%u st%08x", fDone, fStage, fStep, fError, fBus.error, fBus.error_status);
  line(text);
  return false;
}

void NativeKeyboard::run()
{
  // The clock vote registers and the apps SMMU have no lock against the other bring-up threads; let those start first.
  IOSleep(kStartDelayMs);
  fStartMs = nowMs();
  char text[120];
  if (!snapshot())
    return;
  fDone = 1;
  if (fStage < 2)
    return;
  if (!enableClocks() || !readEngine())
    return;
  fDone = 2;
  if (fStage < 3)
    return;
  if (!setupPins() || (fRails && !setupRails()) || !setupBus() || !powerOn())
    return;
  fDone = 3;
  snprintf(text, sizeof(text), "KBD rom id%02x state%02x (expect c8, 00)", fRomId & 0xff, fRomState & 0xff);
  line(text);
  if (fStage < 4)
    return;
  if (!loadImage())
    return;
  fDone = 4;
  snprintf(text, sizeof(text), "KBD rom hdr%02x verify%02x (expect 01, 03), image %u bytes in %u ms", fRomHeader & 0xff, fRomVerified & 0xff, fCodeBytes,
           fImageMs);
  line(text);
  serve();
}

bool NativeKeyboard::snapshot()
{
  fStep = 10;
  if (!fGccLow.open(kGcc, 0x1000) || !fGcc1e.open(kGcc + 0x1e000, 0x1000) || !fGcc23.open(kGcc + 0x23000, 0x1000) ||
      !fGccVote.open(kGcc + 0x52000, 0x1000))
    return fail(10, -12);
  for (unsigned i = 0; i < PinCount; ++i)
    if (!fPin[i].open(kTlmm + tb_tlmm_ctl(kPins[i]), 0x1000))
      return fail(11, static_cast<int32_t>(kPins[i]));
  snapshotRails();
  snapshotPins();
  snapshotClocks();
  return true;
}

// Read-only: the PMIC peripherals of the three LDOs that can feed the controller. A read failure is printed, not fatal.
void NativeKeyboard::snapshotRails()
{
  static const struct { const char *name; uint8_t peripheral; } rails[] = {{"L6B", 0xc6}, {"L12B", 0xcc}, {"L13B", 0xcd}};
  char text[120];
  if (!fSpmi.coreRegion.open(kSpmiCore, kSpmiCoreBytes) || !fSpmi.cnfgRegion.open(kSpmiCnfg, kSpmiCnfgBytes) ||
      !fSpmi.obsRegion.open(kSpmiObs, kSpmiObsBytes)) {
    line("KBD rails: cannot map the SPMI arbiter");
    return;
  }
  fArbiter.io = &fSpmi;
  fArbiter.coreBytes = kSpmiCoreBytes;
  fArbiter.cnfgBytes = kSpmiCnfgBytes;
  fArbiter.obsBytes = kSpmiObsBytes;
  fArbiter.ee = 0;
  if (sheng_keys::probe(fArbiter) != sheng_keys::Status::ok) {
    line("KBD rails: SPMI arbiter not recognised");
    return;
  }
  for (const auto &rail : rails) {
    const int apid = sheng_keys::findApid(fArbiter, 1, rail.peripheral);
    static const uint8_t regs[] = {0x04, 0x05, 0x08, 0x09, 0x40, 0x41, 0x45, 0x46};
    uint8_t v[sizeof(regs)] = {};
    int status = 0;
    for (unsigned i = 0; i < sizeof(regs); ++i) {
      const sheng_keys::Status s = sheng_keys::readByte(fArbiter, apid, static_cast<uint16_t>(rail.peripheral << 8 | regs[i]), &v[i]);
      if (s != sheng_keys::Status::ok)
        status = static_cast<int>(s);
    }
    snprintf(text, sizeof(text), "KBD rail %s a%d rc%d type%02x/%02x st%02x/%02x v%02x%02x mode%02x en%02x", rail.name, apid, status, v[0], v[1], v[2], v[3],
             v[5], v[4], v[6], v[7]);
    line(text);
  }
}

void NativeKeyboard::snapshotPins()
{
  char text[120];
  for (unsigned first = 0; first < PinCount; first += 4) {
    int n = 0;
    n += snprintf(text + n, sizeof(text) - static_cast<size_t>(n), "KBD pin");
    for (unsigned i = first; i < first + 4 && i < PinCount; ++i)
      n += snprintf(text + n, sizeof(text) - static_cast<size_t>(n), " %u:%08x/%x", kPins[i], rd32(fPin[i].va, kCtl), rd32(fPin[i].va, kIo));
    line(text);
  }
  snprintf(text, sizeof(text), "KBD pin 62 intr cfg%08x status%x", rd32(fPin[PinIrq].va, kIntrCfg), rd32(fPin[PinIrq].va, kIntrStatus));
  line(text);
}

void NativeKeyboard::snapshotClocks()
{
  char text[120];
  uint32_t halt[kHaltCount];
  for (unsigned i = 0; i < kHaltCount; ++i)
    halt[i] = rd32((kHalts[i] >> 12) == 0x1e ? fGcc1e.va : fGcc23.va, kHalts[i] & 0xfff);
  const uintptr_t rcg = fGcc1e.va + (kRcg & 0xfff);
  snprintf(text, sizeof(text), "KBD gcc vote%08x h%08x %08x %08x %08x %08x dfs%08x", rd32(fGccVote.va, kVote & 0xfff), halt[0], halt[1], halt[2], halt[3], halt[4],
           rd32(rcg, TB_RCG_DFSR_CMD));
  line(text);
  int n = snprintf(text, sizeof(text), "KBD dfs kHz");
  for (unsigned l = 0; l < TB_DFS_LEVELS; ++l) {
    const uint64_t hz = tb_dfs_level_hz(rd32(rcg, TB_RCG_PERF_CFG(l)), rd32(rcg, TB_RCG_PERF_M(l)), rd32(rcg, TB_RCG_PERF_N(l)));
    n += snprintf(text + n, sizeof(text) - static_cast<size_t>(n), " %u", static_cast<unsigned>(hz / 1000));
  }
  line(text);
}

bool NativeKeyboard::enableClocks()
{
  fStep = 20;
  wr32(fGccVote.va, kVote & 0xfff, rd32(fGccVote.va, kVote & 0xfff) | kVoteBits);
  bool running = false;
  for (unsigned tries = 0; tries < 2000 && !running; ++tries) {
    running = true;
    for (uint32_t reg : kHalts) {
      const uint32_t v = rd32((reg >> 12) == 0x1e ? fGcc1e.va : fGcc23.va, reg & 0xfff);
      if ((v & TB_CBCR_CLK_OFF) && !(v & 2u))   // a branch under hardware clock gating reads off while idle
        running = false;
    }
    if (!running)
      IODelay(10);
  }
  snapshotClocks();
  if (!running)
    return fail(20, -60);   // never touch an engine without running clocks
  wr32(fPin[PinSda].va, kCtl, 1u << 2);   // qup2_se1, no pull, 2 mA
  wr32(fPin[PinScl].va, kCtl, 1u << 2);
  return true;
}

bool NativeKeyboard::readEngine()
{
  fStep = 21;
  if (!fSe.open(kSe, TB_SE_SIZE) || !fWrapper.open(kWrapper, 0x2000))
    return fail(21, -12);
  char text[120];
  const uint32_t revision = rd32(fSe.va, GENI_FW_REVISION);
  snprintf(text, sizeof(text), "KBD se proto%u fw%08x if%08x clk%08x scl%08x hw%08x/%08x wrap%08x", (revision >> 8) & 0xff, revision, rd32(fSe.va, GENI_IF_DISABLE),
           rd32(fSe.va, GENI_CLK_CFG), rd32(fSe.va, GENI_SCL_COUNTERS), rd32(fSe.va, GENI_HW_PARAM_0), rd32(fSe.va, GENI_HW_PARAM_1), rd32(fWrapper.va, 0x4));
  line(text);
  return true;
}

bool NativeKeyboard::setupPins()
{
  fStep = 30;
  wr32(fPin[PinIrq].va, kIntrCfg, rd32(fPin[PinIrq].va, kIntrCfg) & ~1u);   // no summary interrupt from this pin, ever
  wr32(fPin[PinReset].va, kIo, 0);
  wr32(fPin[PinReset].va, kCtl, 1u << 9);          // output low, no pull, 2 mA
  wr32(fPin[PinSleep].va, kIo, 0);
  wr32(fPin[PinSleep].va, kCtl, 1u << 9 | 1u);     // output low, pull-down
  wr32(fPin[PinStatus].va, kCtl, 0);               // input, no pull
  wr32(fPin[PinIrq].va, kCtl, rd32(fPin[PinIrq].va, kCtl) & 3u);   // input, pull as found
  return true;
}

// The two supplies, one request per register of a rail (mode 7 HPM, voltage in mV, enable), as the RPMh regulator driver sends them.
// The voltages are fixed here: 3296 mV is the mainline DT value, and a neighbouring tablet's 3304 mV reset the SoC.
bool NativeKeyboard::setupRails()
{
  fStep = 31;
  uint8_t *database = static_cast<uint8_t *>(IOMalloc(kCmdDbBytes));
  if (!database)
    return fail(31, -12);
  Region db, rsc;
  bool ok = db.open(kCmdDb, kCmdDbBytes) && rsc.open(kRsc, 0x10000);
  if (!ok)
    fail(31, -12);
  static const struct { const char *name; uint32_t millivolts; } rails[] = {{"ldob12", 1800}, {"ldob13", 3296}};
  QcomRpmhResource resources[2] = {};
  if (ok) {
    for (uint32_t i = 0; i < kCmdDbBytes; i += 4) {
      const uint32_t word = rd32(db.va, i);
      memcpy(database + i, &word, 4);
    }
    for (unsigned i = 0; ok && i < 2; ++i)
      if (qcom_cmd_db_find(database, kCmdDbBytes, rails[i].name, &resources[i])) {
        ok = false;
        fail(31, 0x100 + static_cast<int32_t>(i));
      }
  }
  IOFree(database, kCmdDbBytes);
  if (!ok)
    return false;
  const QcomRscLayout layout = qcom_rsc_layout(rd32(rsc.va, 0));
  const uint32_t tcs = kRscTcs + layout.tcs_stride * kTcs;
  // TCS 2 also serves the USB companion, as it does upstream, where a lock covers it. No common lock exists here: wait for it to go idle.
  auto idle = [&]() {
    for (unsigned tries = 0; tries < 200; ++tries) {
      if (rd32(rsc.va, tcs + layout.status))
        return true;
      IOSleep(1);
    }
    return false;
  };
  if (!idle())
    return fail(32, -16);
  auto send = [&](uint32_t address, uint32_t value) {
    if (!idle())
      return false;
    QcomRpmhWrite writes[16];
    const unsigned count = qcom_rpmh_active_request(&layout, kTcs, &address, &value, 1, writes, 16);
    for (unsigned i = 0; i < count; ++i)
      wr32(rsc.va, kRscTcs + writes[i].offset, writes[i].value);
    bool done = false;
    for (unsigned tries = 0; tries < 50 && !done; ++tries) {
      IOSleep(1);
      done = rd32(rsc.va, kRscTcs + layout.irq_status) & (1u << kTcs);
    }
    const unsigned finish = qcom_rpmh_finish(&layout, kTcs, writes, 16);
    for (unsigned i = 0; i < finish; ++i)
      wr32(rsc.va, kRscTcs + writes[i].offset, writes[i].value);
    return done;
  };
  char text[120];
  for (unsigned i = 0; i < 2; ++i) {
    const bool done = send(resources[i].address + 8, 7) && send(resources[i].address, rails[i].millivolts) && send(resources[i].address + 4, 1);
    snprintf(text, sizeof(text), "KBD vote %s %u mV at %05x %s", rails[i].name, rails[i].millivolts, resources[i].address, done ? "done" : "no completion");
    line(text);
    if (!done)
      return fail(33, static_cast<int32_t>(i));
  }
  return true;
}

// The engine at the chosen speed from 19.2 MHz (i2c-qcom-geni.c: 100 kHz divider 7, high 10, low 12, cycle 26; 400 kHz 2, 5, 11, 22; 1 MHz 1, 2, 8, 18).
bool NativeKeyboard::setupBus()
{
  fStep = 34;
  const uintptr_t se = fSe.va, rcg = fGcc1e.va + (kRcg & 0xfff);
  unsigned level = 0xff;
  for (unsigned l = 0; l < TB_DFS_LEVELS && level == 0xff; ++l)
    if (tb_dfs_level_hz(rd32(rcg, TB_RCG_PERF_CFG(l)), rd32(rcg, TB_RCG_PERF_M(l)), rd32(rcg, TB_RCG_PERF_N(l))) == 19200000ULL)
      level = l;
  const uint32_t protocol = (rd32(se, GENI_FW_REVISION) >> 8) & 0xff;
  if (protocol != GENI_PROTO_I2C)
    return fail(34, static_cast<int32_t>(0x200 | protocol));
  if (level != 0xff) {
    const uint32_t divider = fKhz == 1000 ? 1 : fKhz == 400 ? 2 : 7;
    const uint32_t counters = fKhz == 1000 ? (2u << 20 | 8u << 10 | 18u) : fKhz == 400 ? (5u << 20 | 11u << 10 | 22u) : (10u << 20 | 12u << 10 | 26u);
    wr32(se, 0x28, rd32(se, 0x28) | 0x7fu);       // clock gating controls on
    wr32(se, 0xe30, rd32(se, 0xe30) | 0xfu);      // DMA clock gating controls on
    wr32(se, 0x24, 0x7fu);                        // default output control
    wr32(se, 0x20, 1u);                           // force default
    wr32(se, 0xe18, 0);                           // no GSI events
    wr32(se, 0x7c, level);
    wr32(se, GENI_CLK_CFG, divider << 4 | 1u);
    wr32(se, GENI_SCL_COUNTERS, counters);
  } else if (!(rd32(se, GENI_CLK_CFG) & 1u) || !rd32(se, GENI_SCL_COUNTERS)) {
    return fail(34, 0x300);   // no 19.2 MHz level and no boot loader configuration to keep
  }
  const GeniIo io = {ioRead, ioWrite, reinterpret_cast<void *>(se)};
  uint32_t config = 0;
  if (geni_i2c_prepare(&fBus, &io, WN8030_ADDRESS, &config))
    return fail(35, static_cast<int32_t>(config));
  char text[120];
  snprintf(text, sizeof(text), "KBD bus level %u, %u kHz, config %08x", level, fKhz, config);
  line(text);
  return true;
}

template <typename Start> int NativeKeyboard::transfer(Start start, uint32_t bytes)
{
  const uint32_t ms = 10 + bytes * 9 * 2 / fKhz;
  if (start(&fBus)) {
    ++fFailures;
    return GeniFailed;
  }
  int result = GeniPending;
  for (unsigned step = 0; step < 4 && result == GeniPending; ++step) {
    uint64_t deadline = 0;
    clock_interval_to_deadline(ms, kMillisecondScale, &deadline);
    while ((result = geni_i2c_poll(&fBus)) == GeniPending && mach_absolute_time() < deadline)
      IODelay(kBusPollUs);
    if (result == GeniPending)
      result = geni_i2c_timeout(&fBus);   // cancel, then abort, then give up
  }
  if (result != GeniDone)
    ++fFailures;
  return result == GeniPending ? GeniFailed : result;
}

bool NativeKeyboard::romWrite(uint8_t value)
{
  return transfer([value](GeniI2c *g) { return geni_i2c_start_write(g, WN8030_REG_WRITE, value); }, 2) == GeniDone;
}

int NativeKeyboard::romRead()
{
  if (transfer([](GeniI2c *g) { return geni_i2c_start_read(g, WN8030_REG_WRITE); }, 2) != GeniDone)
    return -1;
  return fBus.data;
}

int NativeKeyboard::romState()
{
  return romWrite(WN8030_ROM_STATE) ? romRead() : -1;
}

bool NativeKeyboard::writeBuffer(const uint8_t *bytes, uint32_t length)
{
  return transfer([bytes, length](GeniI2c *g) { return geni_i2c_start_write_buffer(g, bytes, length); }, length) == GeniDone;
}

bool NativeKeyboard::readFrame()
{
  static const uint8_t reg = WN8030_REG_READ;
  uint8_t *frame = fFrame;
  return transfer([frame](GeniI2c *g) { return geni_i2c_start_read_buffer(g, &reg, 1, frame, WN8030_FRAME_BYTES); }, 1 + WN8030_FRAME_BYTES) == GeniDone;
}

bool NativeKeyboard::control(uint8_t command, uint8_t value)
{
  uint8_t out[1 + WN8030_COMMAND_BYTES];
  const uint32_t n = wn8030_control(out, command, &value, 1);
  return n && writeBuffer(out, n);
}

bool NativeKeyboard::powerOn()
{
  fStep = 36;
  IOSleep(10);                                 // supplies settle with the controller in reset
  wr32(fPin[PinReset].va, kIo, 2u);            // reset released
  IOSleep(20);                                 // the boot ROM starts
  wr32(fPin[PinSleep].va, kIo, 2u);            // sleep released
  IOSleep(5);
  if (!romWrite(WN8030_ROM_ID))
    return fail(36, 1);
  fRomId = romRead();
  fRomState = romState();
  if (fRomId != static_cast<int>(WN8030_BOOT_ID))
    return fail(37, fRomId);
  if (fRomState != Wn8030RomIdle)
    return fail(38, 0x100 | fRomState);
  return true;
}

bool NativeKeyboard::loadImage()
{
  fStep = 40;
  if (!fFirmware)
    return fail(40, -2);
  const uint64_t start = nowMs();
  uint32_t n = wn8030_header_command(fCommand, fFirmware, fCodeBytes);
  if (!writeBuffer(fCommand, n))
    return fail(41, -10);
  fRomHeader = romState();
  if (fRomHeader != Wn8030RomHeader)
    return fail(41, 0x100 | (fRomHeader & 0xff));
  for (uint32_t offset = 0, next = 0; offset < fCodeBytes; offset = next) {
    n = wn8030_data_command(fCommand, fFirmware, fCodeBytes, offset, &next);
    if (!n || !writeBuffer(fCommand, n))
      return fail(42, static_cast<int32_t>(0x10000 | offset));
  }
  if (!romWrite(WN8030_ROM_VERIFY))
    return fail(43, -11);
  IOSleep(10);
  fRomVerified = romState();
  if (fRomVerified != Wn8030RomVerified)
    return fail(43, 0x200 | (fRomVerified & 0xff));
  // Rising edges of GPIO 62 latch in INTR_STATUS with the raw status bit set; the enable bit stays clear, so the shared TLMM summary interrupt never fires for it.
  uint32_t cfg = rd32(fPin[PinIrq].va, kIntrCfg) & ~((7u << 5) | (3u << 2) | (1u << 1) | 1u);
  cfg |= (3u << 5) | (1u << 2) | (1u << 1) | (1u << 4);
  wr32(fPin[PinIrq].va, kIntrCfg, cfg);
  wr32(fPin[PinIrq].va, kIntrStatus, 0);
  if (!romWrite(WN8030_ROM_BOOT))
    return fail(44, -12);
  IOSleep(50);
  fImageMs = static_cast<uint32_t>(nowMs() - start);
  return true;
}

void NativeKeyboard::sendReport(const uint8_t *bytes)
{
  if (!fHid)
    return;
  IOBufferMemoryDescriptor *report = IOBufferMemoryDescriptor::withBytes(bytes, 8, kIODirectionNone);
  if (report) {
    fHid->handleReport(report);
    report->release();
  }
}

// Stage 4 only: stage 5 can carry a typed password.
void NativeKeyboard::logFrame()
{
  if (fStage != 4 || fFrames > kFramesLogged)
    return;
  char text[120];
  int n = snprintf(text, sizeof(text), "KBD frame %u:", fFrames);
  for (unsigned i = 0; i < 16; ++i)
    n += snprintf(text + n, sizeof(text) - static_cast<size_t>(n), " %02x", fFrame[i]);
  line(text);
}

bool NativeKeyboard::handleFrame()
{
  if (!readFrame())
    return false;
  ++fFrames;
  logFrame();
  Wn8030Frame frame;
  switch (wn8030_parse(fFrame, &frame)) {
  case Wn8030Keyboard:
    ++fKeyReports;
    sendReport(frame.report + 1);   // the report without its ID is the boot keyboard report
    break;
  case Wn8030Consumer:
    ++fConsumer;
    break;
  case Wn8030Touchpad:
    ++fTouchpad;
    break;
  case Wn8030Attach:
    if (frame.attached == 1 && !fAttached) {
      fAttached = true;
      ++fAttaches;
      control(WN8030_CMD_TOUCHPAD, 0);
      control(WN8030_CMD_INDICATOR, WN8030_LED_CAPS_OFF);
      line("KBD cover attached");
    } else if (frame.attached == 0 && fAttached) {
      fAttached = false;
      sendReport(kReleased);
      line("KBD cover detached");
    }
    break;
  case Wn8030AuthRequest:
  case Wn8030AuthInit:
  case Wn8030AuthChallenge:
    ++fAuth;
    break;
  case Wn8030Invalid:
    ++fInvalid;
    break;
  default:
    ++fOther;
    break;
  }
  return true;
}

void NativeKeyboard::printServing()
{
  char text[160];
  snprintf(text, sizeof(text), "KBD serve att%u/%u edges%u frames%u bad%u key%u cons%u tp%u auth%u other%u fail%u ka%u gpio62:%u 56:%u hall%u%u", fAttached, fAttaches, fEdges,
           fFrames, fInvalid, fKeyReports, fConsumer, fTouchpad, fAuth, fOther, fFailures, fKeepAlives, rd32(fPin[PinIrq].va, kIo) & 1, rd32(fPin[PinStatus].va, kIo) & 1,
           rd32(fPin[PinLid].va, kIo) & 1, rd32(fPin[PinTablet].va, kIo) & 1);
  line(text);
}

void NativeKeyboard::serve()
{
  uint32_t consecutive = 0;
  const uint64_t started = nowMs();
  uint64_t nextReport = started, nextPollRead = started + fPollReadMs, nextKeepAlive = 0;
  bool wasAttached = false;
  for (;;) {
    bool read = false;
    if (rd32(fPin[PinIrq].va, kIntrStatus) & 1u) {
      wr32(fPin[PinIrq].va, kIntrStatus, 0);
      ++fEdges;
      read = true;
    }
    uint64_t now = nowMs();
    if (fPollReadMs && now >= nextPollRead) {
      nextPollRead = now + fPollReadMs;
      read = true;
    }
    if (read) {
      consecutive = handleFrame() ? 0 : consecutive + 1;
      if (consecutive == kMaxFailures) {
        fail(50, static_cast<int32_t>(consecutive));
        printServing();
        return;
      }
      now = nowMs();
    }
    if (fAttached && !wasAttached)
      nextKeepAlive = now + kKeepAliveMs;
    wasAttached = fAttached;
    if (fAttached && now >= nextKeepAlive) {
      nextKeepAlive = now + kKeepAliveMs;
      if (control(WN8030_CMD_KEYBOARD_POWER, 1))
        ++fKeepAlives;
    }
    if (now - started <= kReportForMs && now >= nextReport) {
      nextReport = now + kReportEveryMs;
      printServing();
    }
    if (!read)
      IOSleep(fAttached ? kPollAttachedMs : kPollIdleMs);
  }
}
