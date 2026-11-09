// SPDX-License-Identifier: GPL-3.0-or-later
// Runs the real platform/sheng/macos26/input/native_keyboard.cpp against simulated hardware: GCC, TLMM, the SPMI observer, a GENI I2C engine, the
// WN8030 controller, the RSC and cmd-db. It proves the ordering and the staging of the front end; it says nothing about the real hardware.
#include "native_keyboard_stubs.h"
#include <array>
#include <deque>
#include <functional>
#include <map>

#define NATIVE_KEYBOARD_HOST_MMIO
static inline void dsb() {}
uint32_t rd32(uintptr_t base, uint32_t reg);
void wr32(uintptr_t base, uint32_t reg, uint32_t value);
#include "../platform/sheng/macos26/input/native_keyboard.cpp"

#define CHECK(x) do { if (!(x)) { printf("FAIL line %d: %s\n", __LINE__, #x); printf("log:\n%s\n", gLog.c_str()); exit(1); } } while (0)

struct Stop {};
int PE_vmm_present = 0;
const void *gIODTPlane = &gIODTPlane;
static std::string gLog;
static std::map<std::string, uint32_t> gArgs;
static uint64_t gNowNs = 0, gEndNs = 0;
struct Timed { uint64_t ns; std::function<void()> action; };
static std::vector<Timed> gTimed;
static void (*gThread)(void *, wait_result_t) = nullptr;
static void *gThreadArg = nullptr;
struct Report { uint64_t ns; std::array<uint8_t, 8> bytes; };
static std::vector<Report> gReports;

enum Region { RegionGcc, RegionTlmm, RegionSpmi, RegionSe, RegionWrapper, RegionRsc, RegionCmdDb, RegionCount };
static unsigned gReads[RegionCount], gWrites[RegionCount];

static constexpr uint32_t kWantVote = 0x2f;   // gcc-sm8550.c: wrap2 core, S AHB, M AHB, core 2x, SE1
static std::map<uint32_t, uint32_t> gGcc;
static bool gClocksStuck = false;
static std::map<uint64_t, uint32_t> gTlmm;
struct PinWrite { uint64_t ns; unsigned pin; uint32_t reg, value; };
static std::vector<PinWrite> gPinWrites;
static std::map<uint32_t, uint8_t> gPmic;
static std::map<uint32_t, uint32_t> gObs;

static void edge62()
{
  const uint64_t pin = 0xf100000ull + 62 * 0x1000;
  const uint32_t cfg = gTlmm[pin + 8];
  if ((cfg & (1u << 4)) && ((cfg >> 2) & 3) == 1)
    gTlmm[pin + 0xc] = 1;
}

static bool high(unsigned pin) { return gTlmm[0xf100000ull + pin * 0x1000ull + 4] & 2u; }

struct Mcu {
  bool present = true, running = false, autoAttach = false;
  uint8_t selected = 0;
  uint8_t state = 0;
  uint32_t code = 0, checksum = 0;
  std::vector<uint8_t> image;
  std::deque<std::array<uint8_t, 68>> frames;
  std::vector<std::pair<uint8_t, uint8_t>> controls;
  unsigned headers = 0, chunks = 0;

  bool powered() const { return present && high(58) && high(57); }
  void queue(const std::array<uint8_t, 68> &frame) { frames.push_back(frame); edge62(); }
  bool write(const std::vector<uint8_t> &b)
  {
    if (!powered())
      return false;
    if (running) {
      if (b.size() == 1 && b[0] == 0x4c)
        return true;
      CHECK(b.size() == 67 && b[0] == 0x5c && b[1] == 0x32 && b[2] == 0 && b[3] == 0x4e && b[4] == 0x31 && b[5] == 0x80 && b[6] == 0x38);
      const uint8_t length = b[8];
      uint8_t sum = 0;
      for (unsigned i = 3; i < 9u + length; ++i) sum = uint8_t(sum + b[i]);
      CHECK(b[9u + length] == sum);
      controls.push_back({b[7], b[9]});
      return true;
    }
    CHECK(b[0] == 0x5c);
    if (b.size() == 1)
      return true;
    if (b.size() == 2) {
      if (b[1] == 4 || b[1] == 5) {
        selected = b[1];
      } else if (b[1] == 8) {
        uint32_t sum = 0;
        for (uint32_t i = 0; i < code; i += 4) {
          uint32_t word = 0;
          for (uint32_t k = 0; k < 4 && i + k < code; ++k) word |= uint32_t(image[i + k]) << (8 * k);
          sum += word;
        }
        state = sum == checksum && image.size() >= code ? 3 : 2;
      } else if (b[1] == 9) {
        CHECK(state == 3);
        running = true;
      } else {
        CHECK(false);
      }
      return true;
    }
    if (b.size() == 66 && b[1] == 6) {
      CHECK(!memcmp(&b[2], "NANO IC", 7));
      uint8_t sum = 0;
      for (unsigned i = 2; i < 65; ++i) sum = uint8_t(sum + b[i]);
      CHECK(b[65] == sum);
      code = b[19] | b[20] << 8 | b[21] << 16 | uint32_t(b[22]) << 24;
      checksum = b[54] | b[55] << 8 | b[56] << 16 | uint32_t(b[57]) << 24;
      image.clear();
      state = 1;
      ++headers;
      return true;
    }
    CHECK(b.size() == 513 && b[1] == 7);
    const uint32_t offset = uint32_t(b[2]) << 24 | b[3] << 16 | b[4] << 8 | b[5], size = uint32_t(b[6]) << 24 | b[7] << 16 | b[8] << 8 | b[9];
    CHECK(offset == image.size() && size && size <= 503);
    image.insert(image.end(), b.begin() + 10, b.begin() + 10 + size);
    ++chunks;
    return true;
  }
  std::vector<uint8_t> read(uint32_t length)
  {
    if (!running) {
      CHECK(length == 1);
      return {selected == 4 ? uint8_t(0xc8) : state};
    }
    std::vector<uint8_t> out(length);
    if (!frames.empty()) {
      memcpy(out.data(), frames.front().data(), length < 68 ? length : 68);
      frames.pop_front();
    }
    return out;
  }
};
static Mcu gMcu;

struct Geni {
  std::map<uint32_t, uint32_t> regs;
  uint32_t status = 0, txLength = 0;
  bool writing = false, hang = false;
  uint8_t address = 0;
  std::vector<uint8_t> tx, pendingRx;
  std::deque<uint32_t> rx;
  unsigned commands = 0;

  void reset() { *this = Geni(); regs[GENI_FW_REVISION] = 3u << 8 | 0x12; regs[GENI_HW_PARAM_0] = 16u << 16; regs[GENI_HW_PARAM_1] = 16u << 16; }
  void finishWrite()
  {
    writing = false;
    if (address == WN8030_ADDRESS && gMcu.write(tx))
      status |= GENI_M_CMD_DONE;
    else
      status |= GENI_M_CMD_DONE | GENI_M_CMD_FAILURE | (1u << 10);
  }
  void write(uint32_t reg, uint32_t value)
  {
    regs[reg] = value;
    switch (reg) {
    case GENI_M_IRQ_CLEAR: status &= ~value; break;
    case GENI_M_CMD_CTRL: status |= value == GENI_CMD_CTRL_CANCEL ? GENI_M_CMD_CANCEL : GENI_M_CMD_ABORT; hang = false; writing = false; break;
    case GENI_TX_WATERMARK:
      if (value && writing) status |= GENI_M_TX_WATERMARK;
      break;
    case GENI_M_CMD0: {
      ++commands;
      const uint32_t op = value >> 27;
      address = (value >> 9) & 0x7f;
      if (op == GENI_OP_WRITE) {
        tx.clear();
        txLength = regs[GENI_TX_LEN];
        writing = true;
      } else {
        CHECK(op == GENI_OP_READ);
        pendingRx = gMcu.read(regs[GENI_RX_LEN]);
      }
      break;
    }
    case GENI_TX_FIFO:
      CHECK(writing);
      for (unsigned i = 0; i < 4 && tx.size() < txLength; ++i) tx.push_back(uint8_t(value >> (8 * i)));
      if (tx.size() == txLength && !hang) finishWrite();
      break;
    default: break;
    }
  }
  uint32_t read(uint32_t reg)
  {
    switch (reg) {
    case GENI_TX_FIFO_STATUS: return 0;
    case GENI_RX_FIFO_STATUS: return uint32_t(rx.size());
    case GENI_RX_FIFO: {
      CHECK(!rx.empty());
      const uint32_t word = rx.front();
      rx.pop_front();
      return word;
    }
    case GENI_M_IRQ_STATUS:
      if (writing && tx.size() < txLength && regs[GENI_TX_WATERMARK]) status |= GENI_M_TX_WATERMARK;
      if (!pendingRx.empty() && rx.empty()) {
        for (unsigned w = 0; w < 16 && !pendingRx.empty(); ++w) {
          uint32_t word = 0;
          for (unsigned i = 0; i < 4 && !pendingRx.empty(); ++i) {
            word |= uint32_t(pendingRx.front()) << (8 * i);
            pendingRx.erase(pendingRx.begin());
          }
          rx.push_back(word);
        }
        status |= GENI_M_RX_WATERMARK;
        if (pendingRx.empty()) status |= GENI_M_CMD_DONE | GENI_M_RX_LAST;
      }
      return status;
    default: return regs[reg];
    }
  }
};
static Geni gGeni;

static std::map<uint32_t, uint32_t> gRsc;
static bool gTcsBusy = false;
struct Vote { uint32_t address, value; };
static std::vector<Vote> gVotes;
static std::vector<uint8_t> gCmdDb;
static constexpr uint32_t kTcsBase = 0xd00 + 672 * 2;

static uint32_t readGcc(uint32_t o)
{
  for (uint32_t halt : kHalts)
    if (o == halt) return !gClocksStuck && (gGcc[0x52010] & kWantVote) == kWantVote ? 0 : 0x80000000u;
  return gGcc[o];
}

uint32_t rd32(uintptr_t base, uint32_t reg)
{
  const uint64_t a = base + reg;
  if (a >= 0x100000 && a < 0x400000) { ++gReads[RegionGcc]; return readGcc(uint32_t(a - 0x100000)); }
  if (a >= 0xf100000 && a < 0xf300000) { ++gReads[RegionTlmm]; return gTlmm[a]; }
  if (a >= 0xc400000 && a < 0xc403000) { ++gReads[RegionSpmi]; uint32_t o = uint32_t(a - 0xc400000); if (!o) return 0x70010000; if (o == 4) return 64; if (o == 0x2000 + 4 * 7) return (1u << 8 | 0xc6) << 8; if (o == 0x2000 + 4 * 8) return (1u << 8 | 0xcc) << 8; if (o == 0x2000 + 4 * 9) return (1u << 8 | 0xcd) << 8; return 0; }
  if (a >= 0xc42d000 && a < 0xc431000) { ++gReads[RegionSpmi]; return 0; }
  if (a >= 0xc440000 && a < 0xc448000) { ++gReads[RegionSpmi]; const uint32_t o = uint32_t(a - 0xc440000); return (o & 0x1f) == 8 ? 1 : gObs[o]; }
  if (a >= 0x884000 && a < 0x888000) { CHECK((gGcc[0x52010] & kWantVote) == kWantVote); ++gReads[RegionSe]; return gGeni.read(uint32_t(a - 0x884000)); }
  if (a >= 0x8c0000 && a < 0x8c2000) { CHECK((gGcc[0x52010] & kWantVote) == kWantVote); ++gReads[RegionWrapper]; return 0x30090000; }
  if (a >= 0x17a20000 && a < 0x17a30000) {
    ++gReads[RegionRsc];
    const uint32_t o = uint32_t(a - 0x17a20000);
    if (!o) return 3u << 16;
    if (o == kTcsBase + 0x28) return gTcsBusy ? 0 : 1;
    return gRsc[o];
  }
  if (a >= 0x81c60000 && a < 0x81c80000) { ++gReads[RegionCmdDb]; uint32_t w = 0; memcpy(&w, gCmdDb.data() + (a - 0x81c60000), 4); return w; }
  printf("read of an unexpected address %llx\n", (unsigned long long)a);
  exit(1);
}

void wr32(uintptr_t base, uint32_t reg, uint32_t value)
{
  const uint64_t a = base + reg;
  if (a >= 0x100000 && a < 0x400000) { ++gWrites[RegionGcc]; gGcc[uint32_t(a - 0x100000)] = value; return; }
  if (a >= 0xf100000 && a < 0xf300000) {
    ++gWrites[RegionTlmm];
    gTlmm[a] = value;
    gPinWrites.push_back({gNowNs, unsigned((a - 0xf100000) / 0x1000), uint32_t((a - 0xf100000) & 0xfff), value});
    return;
  }
  if (a >= 0xc440000 && a < 0xc448000) {
    ++gWrites[RegionSpmi];
    const uint32_t o = uint32_t(a - 0xc440000);
    CHECK((o & 0x1f) == 0);
    const unsigned apid = o / 0x20;
    CHECK(apid >= 7 && apid <= 9);
    const uint32_t periph = apid == 7 ? 0xc6 : apid == 8 ? 0xcc : 0xcd;
    gObs[o + 0x18] = gPmic[periph << 8 | ((value >> 4) & 0xff)];
    return;
  }
  if (a >= 0x884000 && a < 0x888000) { CHECK((gGcc[0x52010] & kWantVote) == kWantVote); ++gWrites[RegionSe]; gGeni.write(uint32_t(a - 0x884000), value); return; }
  if (a >= 0x17a20000 && a < 0x17a30000) {
    ++gWrites[RegionRsc];
    const uint32_t o = uint32_t(a - 0x17a20000);
    gRsc[o] = value;
    if (o == kTcsBase + 0x24 && (value & (1u << 24))) {
      gVotes.push_back({gRsc[kTcsBase + 0x38], gRsc[kTcsBase + 0x3c]});
      gRsc[0xd00 + 4] |= 1u << 2;
    }
    if (o == 0xd00 + 8) gRsc[0xd00 + 4] &= ~value;
    return;
  }
  printf("write of an unexpected address %llx\n", (unsigned long long)a);
  exit(1);
}

void IOLog(const char *format, ...)
{
  char line[400];
  va_list list;
  va_start(list, format);
  vsnprintf(line, sizeof(line), format, list);
  va_end(list);
  gLog += line;
}

static void advance(uint64_t ns)
{
  gNowNs += ns;
  for (size_t i = 0; i < gTimed.size();) {
    if (gTimed[i].ns <= gNowNs) {
      auto action = gTimed[i].action;
      gTimed.erase(gTimed.begin() + long(i));
      action();
    } else {
      ++i;
    }
  }
  if (gNowNs > gEndNs) throw Stop{};
}
void IOSleep(unsigned ms) { advance(uint64_t(ms) * 1000000); }
void IODelay(unsigned us) { advance(uint64_t(us) * 1000); }
uint64_t mach_absolute_time() { return gNowNs; }

IOReturn IOHIDDevice::handleReport(IOMemoryDescriptor *report)
{
  CHECK(report->bytes.size() == 8);
  Report r;
  r.ns = gNowNs;
  memcpy(r.bytes.data(), report->bytes.data(), 8);
  gReports.push_back(r);
  return kIOReturnSuccess;
}

kern_return_t kernel_thread_start(void (*continuation)(void *, wait_result_t), void *argument, thread_t *thread)
{
  gThread = continuation;
  gThreadArg = argument;
  *thread = nullptr;
  return KERN_SUCCESS;
}

bool PE_parse_boot_argn(const char *name, void *destination, int)
{
  auto it = gArgs.find(name);
  if (it == gArgs.end()) return false;
  *static_cast<uint32_t *>(destination) = it->second;
  return true;
}

static IORegistryEntry gNode;
static OSData gFirmwareProperty;
static bool gHaveFirmware = false;
IORegistryEntry *IORegistryEntry::fromPath(const char *path, const void *plane)
{
  CHECK(plane == gIODTPlane && !strcmp(path, "/arm-io/native-keyboard"));
  if (!gHaveFirmware) return nullptr;
  gNode.property = &gFirmwareProperty;
  return &gNode;
}

static std::vector<uint8_t> makeImage()
{
  std::vector<uint8_t> image(0xa000);
  const uint32_t code = 1100;
  memcpy(&image[0x7fc0], "NANO IC", 7);
  image[0x7fc0 + 48] = uint8_t(code);
  image[0x7fc0 + 49] = uint8_t(code >> 8);
  uint32_t sum = 0;
  for (uint32_t i = 0; i < code; ++i) {
    image[0x8000 + i] = uint8_t(i * 7 + 3);
    sum += image[0x8000 + i];
  }
  for (unsigned i = 0; i < 4; ++i) image[0x7fc0 + 52 + i] = uint8_t(sum >> (8 * i));
  return image;
}

static void putDbEntry(std::vector<uint8_t> &db, size_t at, const char *name, uint32_t address)
{
  memcpy(&db[at], name, strlen(name));
  memcpy(&db[at + 16], &address, 4);
}

static void setup(std::map<std::string, uint32_t> args, uint64_t endMs)
{
  gArgs = std::move(args);
  gNowNs = 0;
  gEndNs = endMs * 1000000;
  gLog.clear();
  gTimed.clear();
  gReports.clear();
  gGcc.clear();
  gTlmm.clear();
  gPinWrites.clear();
  gPmic.clear();
  gObs.clear();
  gRsc.clear();
  gVotes.clear();
  gClocksStuck = false;
  gTcsBusy = false;
  gThread = nullptr;
  memset(gReads, 0, sizeof(gReads));
  memset(gWrites, 0, sizeof(gWrites));
  gMcu = Mcu();
  gGeni.reset();
  for (unsigned l = 0; l < 8; ++l) gGcc[0x1e148 + 0x1c + 4 * l] = l == 2 ? 0 : 1u << 8 | 5;
  gPmic[0xcd46] = 0x80;
  gPmic[0xcd40] = 0xe0;
  gPmic[0xcd41] = 0x0c;
  gCmdDb.assign(0x20000, 0);
  uint32_t magic = 0x0c0330db;
  memcpy(&gCmdDb[4], &magic, 4);
  gCmdDb[8] = 3;       // slave id
  gCmdDb[14] = 2;      // entry count; header and data offsets are 0
  putDbEntry(gCmdDb, 144, "ldob12", 0x40100);
  putDbEntry(gCmdDb, 168, "ldob13", 0x40200);
  gHaveFirmware = false;
  gFirmwareProperty.value = makeImage();
}

static bool run()
{
  auto *driver = new NativeKeyboard;
  if (!driver->start(nullptr)) return false;
  CHECK(gThread);
  try {
    gThread(gThreadArg, 0);
  } catch (Stop &) {
  }
  return true;
}

static bool logged(const char *text) { return gLog.find(text) != std::string::npos; }

static std::array<uint8_t, 68> frame(std::initializer_list<uint8_t> bytes)
{
  std::array<uint8_t, 68> f{};
  size_t i = 0;
  for (uint8_t b : bytes) f[i++] = b;
  return f;
}
static std::array<uint8_t, 68> keyFrame(uint8_t modifiers, uint8_t key) { return frame({0x57, 0, 9, 5, modifiers, 0, key}); }
static std::array<uint8_t, 68> attachFrame(uint8_t bits) { return frame({0x57, 0, 9, 0x22, 0, 0x38, 0x80, 0xa2, 0, 0, 0, 0, bits}); }
static std::array<uint8_t, 68> authFrame() { return frame({0x57, 0, 9, 0x24, 0, 0x38, 0x80, 0x24}); }

static void whenRunning(uint64_t atMs, std::function<void()> action) { gTimed.push_back({atMs * 1000000, std::move(action)}); }

int main()
{
  // Default off, wrong stage, and never in a VM.
  setup({}, 100);
  CHECK(!run() && gLog.empty());
  setup({{"sheng-kbd", 6}}, 100);
  CHECK(!run());
  setup({{"sheng-kbd", 1}}, 100);
  PE_vmm_present = 1;
  CHECK(!run() && logged("not driven in a VM"));
  PE_vmm_present = 0;

  // Stage 1 writes only SPMI observer read commands and touches no engine.
  setup({{"sheng-kbd", 1}}, 20000);
  CHECK(run());
  CHECK(gWrites[RegionGcc] == 0 && gWrites[RegionTlmm] == 0 && gWrites[RegionRsc] == 0);
  CHECK(gReads[RegionSe] == 0 && gWrites[RegionSe] == 0 && gReads[RegionWrapper] == 0 && gReads[RegionCmdDb] == 0);
  CHECK(gWrites[RegionSpmi] == 3 * 8);
  CHECK(logged("KBD rail L13B a9 rc0") && logged("v0ce0 mode00 en80") && logged("KBD pin 56:") && logged("KBD gcc vote") && logged("19200"));
  CHECK(logged("stage 1 started"));

  // Stage 2 votes, sets only the bus pins and reads the engine identity without a command.
  setup({{"sheng-kbd", 2}}, 20000);
  CHECK(run());
  CHECK((gGcc[0x52010] & kWantVote) == kWantVote && gGeni.commands == 0 && gWrites[RegionSe] == 0);
  for (const PinWrite &w : gPinWrites) CHECK((w.pin == 60 || w.pin == 61) && w.value == (1u << 2));
  CHECK(gPinWrites.size() == 2 && logged("KBD se proto3"));

  // Clocks that never run: the engine is not touched (the simulator would fail the access only without votes, so count it).
  setup({{"sheng-kbd", 2}}, 20000);
  gClocksStuck = true;
  CHECK(run());
  CHECK(gReads[RegionSe] == 0 && logged("KBD fail") && logged("err-60"));

  // Stage 3: the reset pulse, then reset released 20 ms before sleep, the boot ROM read; no RPMh and no image.
  setup({{"sheng-kbd", 3}}, 20000);
  CHECK(run());
  uint64_t resetLow = 0, resetHigh = 0, sleepHigh = 0;
  for (const PinWrite &w : gPinWrites) {
    if (w.pin == 58 && w.reg == 4) (w.value & 2 ? resetHigh : resetLow) = w.ns;
    if (w.pin == 57 && w.reg == 4 && (w.value & 2)) sleepHigh = w.ns;
  }
  CHECK(resetLow && resetHigh >= resetLow + 10 * 1000000 && sleepHigh == resetHigh + 20 * 1000000);
  CHECK(logged("KBD rom idc8 state00") && gWrites[RegionRsc] == 0 && !gMcu.running && gMcu.headers == 0);
  CHECK(gGeni.regs[GENI_SCL_COUNTERS] == (2u << 20 | 8u << 10 | 18u) && gGeni.regs[0x7c] == 2 && gGeni.regs[GENI_CLK_CFG] == (1u << 4 | 1u));

  // The speed option selects the mainline timing table.
  setup({{"sheng-kbd", 3}, {"sheng-kbd-khz", 100}}, 20000);
  CHECK(run());
  CHECK(gGeni.regs[GENI_SCL_COUNTERS] == (10u << 20 | 12u << 10 | 26u) && gGeni.regs[GENI_CLK_CFG] == (7u << 4 | 1u));

  // No controller on the bus: a NACK stops the sequence.
  setup({{"sheng-kbd", 3}}, 20000);
  gMcu.present = false;
  CHECK(run());
  CHECK(logged("KBD fail") && !logged("KBD rom id"));

  // A wedged engine ends through cancel and the failure report instead of hanging.
  setup({{"sheng-kbd", 3}}, 20000);
  gGeni.hang = true;
  CHECK(run());
  CHECK(logged("KBD fail"));

  // The rail vote is separate, in order, and only at the fixed voltages.
  setup({{"sheng-kbd", 3}, {"sheng-kbd-rails", 1}}, 20000);
  CHECK(run());
  CHECK(gVotes.size() == 6);
  const uint32_t expected[6][2] = {{0x40108, 7}, {0x40100, 1800}, {0x40104, 1}, {0x40208, 7}, {0x40200, 3296}, {0x40204, 1}};
  for (unsigned i = 0; i < 6; ++i) CHECK(gVotes[i].address == expected[i][0] && gVotes[i].value == expected[i][1]);
  CHECK(logged("KBD vote ldob13 3296 mV") && logged("KBD rom idc8"));

  // A busy TCS is left alone.
  setup({{"sheng-kbd", 3}, {"sheng-kbd-rails", 1}}, 20000);
  gTcsBusy = true;
  CHECK(run());
  CHECK(gVotes.empty() && logged("KBD fail") && gMcu.headers == 0);

  // Stage 4 without the image stops after the boot ROM.
  setup({{"sheng-kbd", 4}}, 20000);
  CHECK(run());
  CHECK(logged("absent or rejected") && logged("KBD rom idc8") && logged("KBD fail") && gMcu.headers == 0);

  // A damaged image is refused before any transfer.
  setup({{"sheng-kbd", 4}}, 20000);
  gHaveFirmware = true;
  gFirmwareProperty.value[0x8000] ^= 1;
  CHECK(run());
  CHECK(logged("absent or rejected") && gMcu.headers == 0);

  // Stage 4: the image goes in, the attach event answers with touchpad off and Caps off, keys are logged, nothing reaches HID, keep-awake every 12 s.
  setup({{"sheng-kbd", 4}}, 30000);
  gHaveFirmware = true;
  whenRunning(4000, [] { gMcu.queue(attachFrame(3)); });
  whenRunning(4200, [] { gMcu.queue(keyFrame(0, 0x04)); });
  whenRunning(4300, [] { gMcu.queue(keyFrame(0, 0)); });
  CHECK(run());
  CHECK(gMcu.running && gMcu.headers == 1 && gMcu.chunks == 3 && logged("KBD rom hdr01 verify03"));
  CHECK(gMcu.controls.size() >= 3 && gMcu.controls[0] == std::make_pair(uint8_t(0x21), uint8_t(0)) && gMcu.controls[1] == std::make_pair(uint8_t(0x2e), uint8_t(0xfc)));
  unsigned keepAlives = 0;
  for (auto &c : gMcu.controls) if (c.first == 0x25) { CHECK(c.second == 1); ++keepAlives; }
  CHECK(keepAlives == 2);
  CHECK(logged("KBD cover attached") && logged("KBD frame 2: 57 00 09 05 00 00 04") && gReports.empty() && logged("KBD serve"));
  CHECK((gTlmm[0xf100000ull + 62 * 0x1000 + 8] & 1u) == 0);   // the summary interrupt stays off

  // Stage 5: key reports reach HID without their ID and are never logged; authentication gets no answer; a detach releases the keys.
  setup({{"sheng-kbd", 5}}, 12000);
  gHaveFirmware = true;
  whenRunning(4000, [] { gMcu.queue(attachFrame(3)); });
  whenRunning(4200, [] { gMcu.queue(keyFrame(2, 0x16)); });
  whenRunning(4300, [] { gMcu.queue(authFrame()); });
  whenRunning(4400, [] { gMcu.queue(keyFrame(0, 0)); });
  whenRunning(5000, [] { gMcu.queue(attachFrame(0)); });
  whenRunning(5100, [] { gMcu.queue(frame({0x12, 0, 0, 5, 0, 0, 0x17})); });
  CHECK(run());
  CHECK(gReports.size() == 3);
  const std::array<uint8_t, 8> press{2, 0, 0x16, 0, 0, 0, 0, 0}, none{};
  CHECK(gReports[0].bytes == press && gReports[1].bytes == none && gReports[2].bytes == none);
  CHECK(!logged("KBD frame") && logged("KBD cover detached") && logged("auth1") && logged("bad1"));
  CHECK(gMcu.controls.size() == 2);

  // Without an edge the driver reads nothing; the polling option reads on a timer.
  setup({{"sheng-kbd", 5}}, 6000);
  gHaveFirmware = true;
  whenRunning(4000, [] { gMcu.frames.push_back(keyFrame(0, 0x05)); });
  CHECK(run());
  CHECK(gReports.empty());
  setup({{"sheng-kbd", 5}, {"sheng-kbd-poll-read", 50}}, 6000);
  gHaveFirmware = true;
  whenRunning(4000, [] { gMcu.frames.push_back(keyFrame(0, 0x05)); });
  CHECK(run());
  CHECK(gReports.size() == 1 && gReports[0].bytes[2] == 0x05);

  // A controller that stops answering ends the serving loop instead of spinning.
  setup({{"sheng-kbd", 5}, {"sheng-kbd-poll-read", 10}}, 20000);
  gHaveFirmware = true;
  whenRunning(4000, [] { gMcu.present = false; });
  CHECK(run());
  CHECK(logged("KBD fail") && logged("step50"));
  printf("PASS\n");
  return 0;
}
