// SPDX-License-Identifier: BSD-2-Clause-Patent
// NativeAdsp: macOS 26 front end for the audio DSP (ADSP, LPASS) of the sheng board. Derived from UniXNU-sheng package/AdspProbe.cpp
// at a68ee4ee67cc71792fbf519de7e3e28072c717b5, which is proven on iPadOS only. Changes from upstream: the images come from the device
// tree (/arm-io/native-adsp, local package only) instead of a file reader, the carveouts are refused if they overlap the kernel's RAM,
// there are three stages instead of two, every import is strong (upstream's weak imports are refused by the storage module audit), and
// the state is printed on the kernel console.
//
// Default off. sheng-adsp selects the stage, hardware only (PE_vmm_present == 0):
//   1  read-only snapshot: SMEM partition and items, the DSP's SMP2P state, AOSS mailbox state, RSC id, firmware node sizes. Reads only.
//   2  stage 1, plus the image digests and ELF headers, the command database lookup and one side-effect-free secure call asking whether
//      the peripheral is supported. Nothing changes state.
//   3  starts the DSP: AOSS load state, RPMh proxy votes, PAS load and reset of the two images, GLINK, the IPC router link.
// sheng-adsp-stop=N stops stage 3 after internal step N (see Stage). sheng-adsp-delay=S waits S seconds before stage 3 starts (default
// 20, so the root mount comes first). sheng-battery selects what happens with the battery manager (native_battery.cpp).
//
// Devicetree facts (checked offline against this tablet's stock vendor_boot DTB for msm-id 0x207): adsp_pas PAS id 1 for adsp.mbn in
// adspslpi 0x9ea00000 + 0x4680000 and id 36 for adsp_dtb.mbn in q6_adsp_dtb 0x9e980000 + 0x80000; SMEM 0x81d00000 + 0x200000 with TCSR
// mutex 3 (0x1f40000, stride 0x1000); SMP2P items 443 (in) and 429 (out), remote pid 2; cmd-db 0x81c60000.
// From upstream, unverified here until the survey runs: IPCC 0x408000 (SPI 229), LPASS client 3 with signal 0 GLINK and 2 SMP2P, AOP
// client 0 signal 0; GLINK items 478, 479, 480; AOSS message RAM 0xc300000 + 0x400; apps RSC DRV 2 at 0x17a20000, TCS block +0xd00, SPI 5.
//
// Design (upstream's): one kernel thread owns every step and all protocol state. Interrupt handlers only acknowledge their hardware,
// record which doorbell rang and wake the thread. Nothing spins on a register in a loop and no lock is held across a wait. Clients
// (ShengAdsp.h) register channels and queue sends from any thread; their callbacks run on this thread.
#include <IOKit/IOService.h>
#include <IOKit/IOLib.h>
#include <IOKit/IOMemoryDescriptor.h>
#include <IOKit/IOBufferMemoryDescriptor.h>
#include <IOKit/IODeviceMemory.h>
#include <IOKit/IODeviceTreeSupport.h>
#include <libkern/c++/OSData.h>
#include <kern/thread.h>
#include <kern/clock.h>
#include <pexpert/pexpert.h>
#include <string.h>
#include "adsp_sha256.h"
extern "C" {
#include "QcomGlink.h"
#include "QcomMdt.h"
#include "QcomQmp.h"
#include "QcomRpmh.h"
#include "QcomScm.h"
#include "QcomSmem.h"
#include "QcomSmp2p.h"
#include "ShengAdsp.h"
void sheng_qrtr_start(void);
int armcpu_spi_attach(uint32_t, bool, void (*)(void *), void *, uint32_t *, uint64_t *);
#if defined(NATIVE_ADSP_HOST)
extern uint64_t gPhysBase, gPhysSize;
#define kernelPhysBase gPhysBase
#define kernelPhysSize gPhysSize
#else
// Bound by symbol name so that no kernel header's declaration of these (a private type) can conflict with this one.
extern uint64_t kernelPhysBase __asm__("_gPhysBase");
extern uint64_t kernelPhysSize __asm__("_gPhysSize");
#endif
bool native_battery_start(unsigned stage);
extern volatile int32_t native_qrtr_stats[16];
extern char native_qrtr_text[96];
}

// [0] stage (see Stage; negative: the step that failed), [1] last error, [2] IPCC interrupts, [3] the DSP's slave-kernel bits (-1 no
// item), [4] items (bit 0 SMP2P in 443, 1 out 429, 2 GLINK 478, 3 479, 4 480, 8 our partition, 9 global partition), [5] QMP state | AOSS
// link bits << 8, [6] RSC DRV id, [7] adsp.mbn KiB, [8] ms from the start call to "ready", [9] GLINK messages in, [10] out, [11] channels
// up (bit per client) | version agreed << 14 | link broken << 15, [12] channel opens nobody serves, [13] last SCM x0, [14] its x1,
// [15] votes: lcx index | lmx index << 8 | RSC done interrupts << 16
extern "C" {volatile int32_t native_adsp_stats[16]; char native_adsp_text[96];}

#if defined(NATIVE_ADSP_HOST)
extern "C" void native_adsp_host_smc(uint64_t registers[8]);
extern "C" uint32_t native_adsp_host_read(const volatile void *address);
extern "C" void native_adsp_host_write(volatile void *address, uint32_t value);
#endif

namespace {
enum Stage {
  StMapped = 1, StSurveyed = 2, StFirmware = 3, StSmp2p = 4, StDoorbells = 5, StLoadState = 6, StRails = 7, StDtb = 8, StLoaded = 9,
  StStarted = 10, StReady = 11, StGlink = 12, StChannel = 13,
};
constexpr uint64_t kSmem = 0x81d00000, kSmemSize = 0x200000, kMutex = 0x1f40000 + 3 * 0x1000, kIpcc = 0x408000, kAoss = 0xc300000;
constexpr uint64_t kRscDrv = 0x17a20000, kRscTcs = 0xd00, kCmdDb = 0x81c60000, kCmdDbSize = 0x20000;
constexpr uint64_t kAdspBase = 0x9ea00000, kAdspSize = 0x4680000, kDtbBase = 0x9e980000, kDtbSize = 0x80000;
constexpr uint32_t kPasAdsp = 1, kPasDtb = 36, kRemotePid = 2, kItemIn = 443, kItemOut = 429, kItemCrash = 423;
constexpr uint32_t kItemGlinkDesc = 478, kItemGlinkTx = 479, kItemGlinkRx = 480, kGlinkFifo = 0x4000;
constexpr uint32_t kSpiIpcc = 229, kSpiRsc = 5;
constexpr uint32_t kFatal = 1u << 0, kReady = 1u << 1, kHandover = 1u << 2;
constexpr uint32_t kEvSmp2p = 1, kEvGlink = 2, kEvAop = 4, kEvRsc = 8, kEvSend = 16;
constexpr unsigned kClients = 4, kQueue = 8;

struct Window {IOMemoryMap *map; volatile uint8_t *va;};
bool mapWindow(Window &w, uint64_t base, uint64_t size, IOOptionBits cache = kIOMapInhibitCache)
{
  IODeviceMemory *memory = IODeviceMemory::withRange(base, size);
  w.map = memory ? memory->map(kIOMapAnywhere | cache) : nullptr;
  if (memory) memory->release();
  w.va = w.map ? reinterpret_cast<volatile uint8_t *>(w.map->getVirtualAddress()) : nullptr;
  return w.va != nullptr;
}
void unmap(Window &w) {if (w.map) w.map->release(); w = {};}
void barrierSy()
{
#if defined(__aarch64__)
  __asm__ volatile("dsb sy" ::: "memory");
#endif
}
#if defined(NATIVE_ADSP_HOST)
uint32_t rd(const Window &w, uint64_t at) {return native_adsp_host_read(w.va + at);}
void wr(const Window &w, uint64_t at, uint32_t value) {native_adsp_host_write(w.va + at, value);}
#else
uint32_t rd(const Window &w, uint64_t at) {return *reinterpret_cast<volatile uint32_t *>(w.va + at);}
void wr(const Window &w, uint64_t at, uint32_t value) {*reinterpret_cast<volatile uint32_t *>(w.va + at) = value; barrierSy();}
#endif
void fail(int step, int error) {native_adsp_stats[1] = error; native_adsp_stats[0] = -step;}
void stage(int value) {native_adsp_stats[0] = value;}
void note(const char *prefix, const char *text)
{
  size_t at = 0;
  for (; prefix[at] && at < sizeof native_adsp_text - 1; ++at) native_adsp_text[at] = prefix[at];
  for (size_t i = 0; text[i] && at < sizeof native_adsp_text - 1; ++i) native_adsp_text[at++] = text[i];
  native_adsp_text[at] = 0;
}

Window gSmemW, gMutex, gIpcc, gAoss, gRsc;
QcomSmem gSmem;
QcomSmemPartition gPart;
QcomSmp2p gP2p;
int gStopEntry = -1;
QcomQmp gQmp;
QcomGlink gGlink;
bool gGlinkStarted;
QcomRscLayout gLayout;
uint32_t gRscIrqBits;
volatile uint32_t gPending, gRscDone;
unsigned gMode, gStopAt, gDelay = 20;
}  // namespace

// ---- clients ----
struct ShengAdspMessage {uint32_t length; uint8_t data[SHENG_ADSP_MAX_MESSAGE];};

struct ShengAdspChannel {
  char name[QCOM_GLINK_NAME];
  uint32_t intent_size, intent_count;
  int initiator;
  ShengAdspClient client;
  void *context;
  uint8_t *storage;
  QcomGlinkChannel *glink;
  ShengAdspMessage *queue;  // kQueue slots
  uint32_t head, count;
};

namespace {
ShengAdspChannel gClients[kClients];
uint32_t gClientCount;
IOLock *gClientLock;

void wake(uint32_t bits)
{
  __atomic_fetch_or(&gPending, bits, __ATOMIC_RELEASE);
  thread_wakeup((event_t)&gPending);
}

// ---- interrupts: acknowledge, record, wake ----
void ipccIrq(void *)
{
  uint32_t bits = 0;
  for (unsigned n = 0; n < 32; ++n) {
    const uint32_t id = rd(gIpcc, QCOM_IPCC_RECV_ID);
    if (id == QCOM_IPCC_NONE) break;
    wr(gIpcc, QCOM_IPCC_RECV_CLEAR, id);
    if (id == qcom_ipcc_id(QCOM_IPCC_CLIENT_LPASS, QCOM_IPCC_SIGNAL_SMP2P)) bits |= kEvSmp2p;
    else if (id == qcom_ipcc_id(QCOM_IPCC_CLIENT_LPASS, QCOM_IPCC_SIGNAL_GLINK_QMP)) bits |= kEvGlink;
    else if (id == qcom_ipcc_id(QCOM_IPCC_CLIENT_AOP, QCOM_IPCC_SIGNAL_GLINK_QMP)) bits |= kEvAop;
  }
  native_adsp_stats[2] = native_adsp_stats[2] + 1;
  if (bits) wake(bits);
}
void rscIrq(void *)
{
  const uint32_t status = rd(gRsc, kRscTcs + gLayout.irq_status) & gRscIrqBits;
  if (!status) return;
  wr(gRsc, kRscTcs + gLayout.irq_clear, status);
  __atomic_fetch_or(&gRscDone, status, __ATOMIC_RELEASE);
  native_adsp_stats[15] = native_adsp_stats[15] + (1 << 16);
  wake(kEvRsc);
}

// Waits for an event up to ms milliseconds; returns the event bits (0: the deadline passed).
uint32_t waitEvents(uint32_t ms)
{
  uint32_t bits = __atomic_exchange_n(&gPending, 0, __ATOMIC_ACQ_REL);
  if (bits) return bits;
  assert_wait_timeout((event_t)&gPending, THREAD_UNINT, ms, kMillisecondScale);
  bits = __atomic_exchange_n(&gPending, 0, __ATOMIC_ACQ_REL);
  if (bits) {clear_wait(current_thread(), THREAD_AWAKENED); return bits;}
  thread_block(THREAD_CONTINUE_NULL);
  return __atomic_exchange_n(&gPending, 0, __ATOMIC_ACQ_REL);
}

void ring(uint32_t client, uint32_t signal) {wr(gIpcc, QCOM_IPCC_SEND, qcom_ipcc_id(client, signal));}
void kickAop(void *) {ring(QCOM_IPCC_CLIENT_AOP, QCOM_IPCC_SIGNAL_GLINK_QMP);}
void kickGlink(void *) {ring(QCOM_IPCC_CLIENT_LPASS, QCOM_IPCC_SIGNAL_GLINK_QMP);}
void kickSmp2p() {ring(QCOM_IPCC_CLIENT_LPASS, QCOM_IPCC_SIGNAL_SMP2P);}

// ---- SMEM helpers (allocation under the TCSR hardware mutex) ----
bool lockSmem()
{
  for (unsigned i = 0; i < 1000; ++i) {
    wr(gMutex, 0, 1);
    if (rd(gMutex, 0) == 1) return true;
    IODelay(10);
  }
  return false;
}
void unlockSmem() {wr(gMutex, 0, 0);}
int allocItem(uint16_t item, uint32_t size)
{
  if (!lockSmem()) return -16;
  const int rc = qcom_smem_alloc(&gSmem, &gPart, item, size);
  unlockSmem();
  return rc == QcomSmemExists ? 0 : rc;
}
volatile uint8_t *item(uint16_t id, uint32_t *size)
{
  uint32_t at = 0, bytes = 0;
  if (qcom_smem_find(&gSmem, &gPart, id, &at, &bytes)) return nullptr;
  if (size) *size = bytes;
  return gSmemW.va + at;
}

void readCrashReason()
{
  QcomSmemPartition global;
  uint32_t at = 0, size = 0;
  if (qcom_smem_partition(&gSmem, QCOM_SMEM_HOST_GLOBAL, QCOM_SMEM_HOST_GLOBAL, &global) ||
      qcom_smem_find(&gSmem, &global, kItemCrash, &at, &size) || !size)
    return;
  char text[80];
  size_t n = 0;
  for (; n < sizeof text - 1 && n < size; ++n) {
    const char c = (char)gSmemW.va[at + n];
    if (!c) break;
    text[n] = c >= 32 && c < 127 ? c : '.';
  }
  text[n] = 0;
  if (n) note("crash: ", text);
}

// ---- the survey: reads only ----
void survey()
{
  int32_t bits = 0;
  if (!qcom_smem_partition(&gSmem, QCOM_SMEM_HOST_APPS, kRemotePid, &gPart)) {
    bits |= 1 << 8;
    const uint16_t ids[5] = {kItemIn, kItemOut, kItemGlinkDesc, kItemGlinkTx, kItemGlinkRx};
    for (unsigned i = 0; i < 5; ++i) if (item(ids[i], nullptr)) bits |= 1 << i;
  }
  QcomSmemPartition global;
  if (!qcom_smem_partition(&gSmem, QCOM_SMEM_HOST_GLOBAL, QCOM_SMEM_HOST_GLOBAL, &global)) bits |= 1 << 9;
  native_adsp_stats[4] = bits;
  native_adsp_stats[3] = -1;
  if (volatile uint8_t *in = item(kItemIn, nullptr)) {
    QcomSmp2p view = {};
    view.in = reinterpret_cast<volatile uint32_t *>(in);
    view.local_pid = 0; view.remote_pid = kRemotePid;
    const int index = qcom_smp2p_check_in(view.in, 0, kRemotePid) ? -1 : qcom_smp2p_find_in(&view, "slave-kernel");
    if (index >= 0) native_adsp_stats[3] = (int32_t)qcom_smp2p_in_value(&view, index);
  }
  const uint32_t up = 0xffff;
  native_adsp_stats[5] = (rd(gAoss, 0) == 0x4d41494cu ? 1 : 0) << 12 | (rd(gAoss, 0xc) == up) << 8 | (rd(gAoss, 0x24) == up) << 9 |
                         (rd(gAoss, 0x14) == up) << 10 | (rd(gAoss, 0x2c) == up) << 11;
  native_adsp_stats[6] = (int32_t)rd(gRsc, 0);
  readCrashReason();
}

void service(uint32_t bits);

// ---- RPMh proxy votes ----
bool readCmdDb(uint8_t *copy)
{
  Window db = {};
  if (!mapWindow(db, kCmdDb, kCmdDbSize)) return false;
  for (uint64_t i = 0; i < kCmdDbSize; i += 4) {const uint32_t w = rd(db, i); memcpy(copy + i, &w, 4);}
  unmap(db);
  return true;
}
QcomRpmhResource gLcx, gLmx;
int gTcs = -1;

// Votes both rails to the given ARC index pair through active TCS 1 and waits up to 100 ms for its done interrupt. Upstream's other RPMh
// clients use TCS 0 (Wi-Fi) and TCS 2 (USB, microphone), so the three never share one. A request whose done interrupt is not seen still
// counts (bit 15 of [15]): upstream observed that such requests take effect.
int vote(uint32_t lcx, uint32_t lmx)
{
  if (gTcs < 0) {
    gTcs = 1;
    gRscIrqBits = 1u << gTcs;
    wr(gRsc, kRscTcs + 0, rd(gRsc, kRscTcs + 0) | gRscIrqBits);  // IRQ_ENABLE for our TCS
  }
  const uint32_t addresses[2] = {gLcx.address, gLmx.address}, values[2] = {lcx, lmx};
  QcomRpmhWrite writes[16];
  const unsigned n = qcom_rpmh_active_request(&gLayout, (unsigned)gTcs, addresses, values, 2, writes, 16);
  if (!n) return -22;
  __atomic_store_n(&gRscDone, 0, __ATOMIC_RELEASE);
  for (unsigned i = 0; i < n; ++i) wr(gRsc, kRscTcs + writes[i].offset, writes[i].value);
  uint64_t deadline;
  clock_interval_to_deadline(100, kMillisecondScale, &deadline);
  while (!(__atomic_load_n(&gRscDone, __ATOMIC_ACQUIRE) & gRscIrqBits) && mach_absolute_time() < deadline) service(waitEvents(20));
  bool done = (__atomic_load_n(&gRscDone, __ATOMIC_ACQUIRE) & gRscIrqBits) != 0;
  if (!done && (rd(gRsc, kRscTcs + gLayout.irq_status) & gRscIrqBits)) done = true;  // one look in case the line is not ours
  const unsigned m = qcom_rpmh_finish(&gLayout, (unsigned)gTcs, writes, 16);
  for (unsigned i = 0; i < m; ++i) wr(gRsc, kRscTcs + writes[i].offset, writes[i].value);
  native_adsp_stats[15] = (native_adsp_stats[15] & ~0xffff) | (int32_t)(lcx | lmx << 8 | (done ? 0 : 1u << 15));
  return 0;
}

// ---- secure world calls ----
struct ScmResult {int64_t x0; uint64_t x1;};
ScmResult smcCall(uint64_t function, uint32_t arginfo, uint64_t a, uint64_t b, uint64_t c, uint64_t *session)
{
#if defined(NATIVE_ADSP_HOST)
  uint64_t r[8] = {function, arginfo, a, b, c, 0, *session, 0};
  native_adsp_host_smc(r);
  *session = r[6];
  return {(int64_t)r[0], r[1]};
#else
  register uint64_t x0 __asm__("x0") = function, x1 __asm__("x1") = arginfo, x2 __asm__("x2") = a, x3 __asm__("x3") = b,
                    x4 __asm__("x4") = c, x5 __asm__("x5") = 0, x6 __asm__("x6") = *session, x7 __asm__("x7") = 0;
  __asm__ volatile("smc #0" : "+r"(x0), "+r"(x1), "+r"(x2), "+r"(x3), "+r"(x4), "+r"(x5), "+r"(x6), "+r"(x7)
                   :: "x8", "x9", "x10", "x11", "x12", "x13", "x14", "x15", "x16", "x17", "memory");
  *session = x6;
  return {(int64_t)x0, x1};
#endif
}
ScmResult scm(uint32_t command, uint32_t arginfo, uint64_t a, uint64_t b, uint64_t c)
{
  ScmResult result = {-1, 0};
  for (unsigned busy = 0; busy < 20; ++busy) {
    uint64_t function = qcom_scm_fn(QCOM_SCM_SVC_PIL, command), session = 0;
    unsigned resumes = 0;
    for (;;) {
      result = smcCall(function, arginfo, a, b, c, &session);
      if (result.x0 != QCOM_SCM_INTERRUPTED || ++resumes > 100000) break;
      function = QCOM_SCM_INTERRUPTED;  // resume the interrupted call in its session
    }
    if (result.x0 != QCOM_SCM_V2_EBUSY) break;
    IOSleep(30);
  }
  native_adsp_stats[13] = (int32_t)result.x0;
  native_adsp_stats[14] = (int32_t)result.x1;
  return result;
}
bool scmOk(const ScmResult &r) {return r.x0 == 0 && r.x1 == 0;}

void cleanToPoc(const void *start, size_t length)
{
#if defined(__aarch64__)
  uint64_t ctr;
  __asm__ volatile("mrs %0, ctr_el0" : "=r"(ctr));
  const uintptr_t line = static_cast<uintptr_t>(4) << ((ctr >> 16) & 15);
  for (uintptr_t a = reinterpret_cast<uintptr_t>(start) & ~(line - 1); a < reinterpret_cast<uintptr_t>(start) + length; a += line)
    __asm__ volatile("dc civac, %0" :: "r"(a) : "memory");
  __asm__ volatile("dsb sy" ::: "memory");
#else
  (void)start; (void)length;
#endif
}

// ---- the images: carried in the device tree by a local package, parsed, metadata handed over, segments written into the carveout ----
struct Image {  // plain data, zero at start: static storage (a few KiB of header table each, kept off the stack)
  const uint8_t *bytes;
  uint64_t size;
  bool usable;
  QcomMdtImage mdt;
  IOBufferMemoryDescriptor *meta;
};
Image gDtbImage, gAdspImage;
OSData *gNodeData[4];  // retained for the life of the system: adsp, adsp digest, dtb, dtb digest

int imageRead(void *context, uint64_t offset, void *to, size_t length)
{
  const Image *image = static_cast<const Image *>(context);
  if (offset > image->size || length > image->size - offset) return -1;
  memcpy(to, image->bytes + offset, length);
  return 0;
}
void releaseMeta(Image &image) {if (image.meta) image.meta->release(); image.meta = nullptr;}

// /arm-io/native-adsp: never part of a build, only present in a local package.
bool findFirmwareNode()
{
  IORegistryEntry *node = IORegistryEntry::fromPath("/arm-io/native-adsp", gIODTPlane);
  if (!node) {
    IOLog("NativeAdsp: no /arm-io/native-adsp node; stage 3 needs a package built with the local ADSP images\n");
    return false;
  }
  static const char *const keys[4] = {"firmware", "firmware-sha256", "firmware-dtb", "firmware-dtb-sha256"};
  bool ok = true;
  for (unsigned i = 0; i < 4; ++i) {
    OSData *value = OSDynamicCast(OSData, node->getProperty(keys[i]));
    if (value) {value->retain(); gNodeData[i] = value;} else ok = false;
  }
  node->release();
  if (ok) {
    gAdspImage.bytes = static_cast<const uint8_t *>(gNodeData[0]->getBytesNoCopy());
    gAdspImage.size = gNodeData[0]->getLength();
    gDtbImage.bytes = static_cast<const uint8_t *>(gNodeData[2]->getBytesNoCopy());
    gDtbImage.size = gNodeData[2]->getLength();
  }
  return ok;
}

// Stage 2 and 3: the images arrived whole and parse as signed firmware.
int checkImages()
{
  if (!gAdspImage.bytes || !gDtbImage.bytes) return -2;
  if (!sheng_adsp::imageIntact(gAdspImage.bytes, gAdspImage.size, static_cast<const uint8_t *>(gNodeData[1]->getBytesNoCopy()), gNodeData[1]->getLength()) ||
      !sheng_adsp::imageIntact(gDtbImage.bytes, gDtbImage.size, static_cast<const uint8_t *>(gNodeData[3]->getBytesNoCopy()), gNodeData[3]->getLength()))
    return -80;
  int rc = qcom_mdt_parse(&gAdspImage.mdt, gAdspImage.size, imageRead, &gAdspImage);
  if (rc) return 1000 - rc;
  rc = qcom_mdt_parse(&gDtbImage.mdt, gDtbImage.size, imageRead, &gDtbImage);
  if (rc) return 2000 - rc;
  gAdspImage.usable = gDtbImage.usable = true;
  return 0;
}

// The carveouts must lie outside the RAM the kernel manages (the secure world locks them after authentication; any later kernel use of
// these pages would fault or reset the board). The packaged window is 12 GiB of 0x8C0000000..0xC00000000.
bool carveoutsOutsideKernel()
{
  const uint64_t base = kernelPhysBase, end = kernelPhysBase + kernelPhysSize;
  const uint64_t regions[3][2] = {{kAdspBase, kAdspSize}, {kDtbBase, kDtbSize}, {kSmem, kSmemSize}};
  for (const auto &r : regions)
    if (r[0] < end && base < r[0] + r[1]) return false;
  return true;
}

// init_image with the metadata in physically contiguous memory, then the memory setup of a relocatable image.
int authenticateHeader(Image &image, uint32_t pas, uint64_t base)
{
  const uint64_t bytes = qcom_mdt_metadata_size(&image.mdt);
  image.meta = IOBufferMemoryDescriptor::inTaskWithPhysicalMask(kernel_task,
                                                                (IOOptionBits)kIODirectionInOut | (IOOptionBits)kIOMemoryPhysicallyContiguous, bytes,
                                                                0xfffffffffffff000ull);
  if (!image.meta || image.meta->prepare() != kIOReturnSuccess) return -12;
  uint8_t *va = static_cast<uint8_t *>(image.meta->getBytesNoCopy());
  if (qcom_mdt_metadata(&image.mdt, imageRead, &image, va, bytes)) return -5;
  cleanToPoc(va, bytes);
  const uint64_t phys = image.meta->getPhysicalSegment(0, nullptr, kIOMemoryMapperNone);
  if (!scmOk(scm(QCOM_SCM_PAS_INIT_IMAGE, qcom_scm_args(2, QCOM_SCM_VAL, QCOM_SCM_RW, 0), pas, phys, 0))) return -1;
  if (image.mdt.relocatable &&
      !scmOk(scm(QCOM_SCM_PAS_MEM_SETUP, qcom_scm_args(3, 0, 0, 0), pas, base, qcom_mdt_span(&image.mdt))))
    return -2;
  return 0;
}
// The mapping is released before the images are authenticated: from then on the secure world owns these pages.
int loadSegments(Image &image, uint64_t base, uint64_t size)
{
  Window region = {};
  if (!mapWindow(region, base, size, kIOMapWriteCombineCache)) return -12;
  int rc = 0;
  for (unsigned i = 0; !rc && i < image.mdt.count; ++i) {
    const QcomMdtSegment &s = image.mdt.segment[i];
    uint64_t at = 0;
    if (!s.load) continue;
    if (qcom_mdt_place(&image.mdt, i, base, size, &at)) {rc = -7; break;}
    uint8_t *to = const_cast<uint8_t *>(region.va) + at;
    if (s.file_size && imageRead(&image, s.file_offset, to, (size_t)s.file_size)) {rc = -5; break;}
    if (s.memory_size > s.file_size) bzero(to + s.file_size, (size_t)(s.memory_size - s.file_size));
  }
  barrierSy();
  unmap(region);
  return rc;
}

// ---- events ----
uint32_t gSlave;
bool gHandedOver;
void serviceSmp2p()
{
  if (!gP2p.in) {
    if (volatile uint8_t *in = item(kItemIn, nullptr)) gP2p.in = reinterpret_cast<volatile uint32_t *>(in);
    else return;
  }
  uint32_t value = 0, changed = 0;
  if (qcom_smp2p_service(&gP2p, "slave-kernel", &value, &changed)) kickSmp2p();
  gSlave = value;
  native_adsp_stats[3] = (int32_t)value;
  if (changed & value & kFatal) readCrashReason();
  if ((value & kHandover) && !gHandedOver) {
    gHandedOver = true;
    vote(0, 0);  // the DSP holds its own votes now
  }
}

void applyClients();
void pumpSends();
void serviceGlink()
{
  if (!gGlinkStarted) return;
  if (!gGlink.rx.data) {
    uint32_t size = 0;
    if (volatile uint8_t *rx = item(kItemGlinkRx, &size)) qcom_glink_set_rx(&gGlink, rx, size);
  }
  qcom_glink_poll(&gGlink);
  pumpSends();
  uint32_t up = 0;
  for (unsigned i = 0; i < gClientCount; ++i) if (gClients[i].glink && gClients[i].glink->up) up |= 1u << i;
  native_adsp_stats[9] = (int32_t)gGlink.rx_messages;
  native_adsp_stats[10] = (int32_t)gGlink.tx_messages;
  native_adsp_stats[11] = (int32_t)(up | (uint32_t)gGlink.version_agreed << 14 | (uint32_t)gGlink.broken << 15);
  native_adsp_stats[12] = (int32_t)gGlink.unknown_opens;
  if (gGlink.unknown_opens && strncmp(native_adsp_text, "crash: ", 7)) note("open: ", gGlink.unknown_names);
  if (gGlink.version_agreed && native_adsp_stats[0] >= StReady && native_adsp_stats[0] < StGlink) stage(StGlink);
  if (up && native_adsp_stats[0] >= StGlink) stage(StChannel);
}
void service(uint32_t bits)
{
  if (bits & kEvAop) qcom_qmp_step(&gQmp);
  if (bits & (kEvSmp2p | kEvGlink)) serviceSmp2p();
  if (bits & (kEvGlink | kEvSend)) {applyClients(); serviceGlink();}
}
// Runs events until done() holds or ms pass; returns done().
template <typename F> bool waitUntil(uint32_t ms, F done)
{
  uint64_t deadline;
  clock_interval_to_deadline(ms, kMillisecondScale, &deadline);
  while (!done()) {
    const uint64_t now = mach_absolute_time();
    if (now >= deadline) return done();
    uint64_t left = 0;
    absolutetime_to_nanoseconds(deadline - now, &left);
    service(waitEvents((uint32_t)(left / 1000000) + 1));
  }
  return true;
}

// ---- client glue (DSP thread side) ----
void clientState(void *context, QcomGlinkChannel *, int up)
{
  ShengAdspChannel *c = static_cast<ShengAdspChannel *>(context);
  if (c->client.state) c->client.state(c->context, up);
}
void clientReceive(void *context, QcomGlinkChannel *, const uint8_t *data, uint32_t length)
{
  ShengAdspChannel *c = static_cast<ShengAdspChannel *>(context);
  if (c->client.receive) c->client.receive(c->context, data, length);
}
void applyClients()
{
  if (!gGlinkStarted) return;
  const QcomGlinkClient glue = {clientState, clientReceive};
  IOLockLock(gClientLock);
  for (unsigned i = 0; i < gClientCount; ++i) {
    ShengAdspChannel &c = gClients[i];
    if (!c.glink) c.glink = qcom_glink_channel(&gGlink, c.name, c.intent_size, c.intent_count, c.storage, &glue, &c, c.initiator);
  }
  IOLockUnlock(gClientLock);
}
void pumpSends()
{
  for (unsigned i = 0; i < gClientCount; ++i) {
    ShengAdspChannel &c = gClients[i];
    if (!c.glink || !c.glink->up) continue;
    for (;;) {
      IOLockLock(gClientLock);
      if (!c.count) {IOLockUnlock(gClientLock); break;}
      ShengAdspMessage *m = &c.queue[c.head];
      IOLockUnlock(gClientLock);
      // Only this thread takes messages off the queue, so m stays valid while the lock is dropped for the send.
      if (qcom_glink_send(&gGlink, c.glink, m->data, m->length) != QcomGlinkOk) break;
      IOLockLock(gClientLock);
      c.head = (c.head + 1) % kQueue;
      c.count--;
      IOLockUnlock(gClientLock);
    }
  }
}

// ---- the bring-up ----
bool attachIrqs()
{
  int rc = 0;
  for (unsigned tries = 0;; ++tries) {  // the GIC driver may not have started yet
    rc = armcpu_spi_attach(32 + kSpiIpcc, false, ipccIrq, nullptr, nullptr, nullptr);
    if (rc != kIOReturnNotReady || tries == 240) break;
    IOSleep(250);
  }
  if (rc) {fail(StDoorbells, rc); return false;}
  if (armcpu_spi_attach(32 + kSpiRsc, false, rscIrq, nullptr, nullptr, nullptr)) native_adsp_stats[15] |= 0x80 << 16;
  const uint32_t enable[3] = {qcom_ipcc_id(QCOM_IPCC_CLIENT_LPASS, QCOM_IPCC_SIGNAL_SMP2P),
                              qcom_ipcc_id(QCOM_IPCC_CLIENT_LPASS, QCOM_IPCC_SIGNAL_GLINK_QMP),
                              qcom_ipcc_id(QCOM_IPCC_CLIENT_AOP, QCOM_IPCC_SIGNAL_GLINK_QMP)};
  for (uint32_t id : enable) wr(gIpcc, QCOM_IPCC_RECV_ENABLE, id);
  return true;
}

void shutdownDsp(bool dtb, bool adsp)
{
  if (adsp) scm(QCOM_SCM_PAS_SHUTDOWN, qcom_scm_args(1, 0, 0, 0), kPasAdsp, 0, 0);
  if (dtb) scm(QCOM_SCM_PAS_SHUTDOWN, qcom_scm_args(1, 0, 0, 0), kPasDtb, 0, 0);
}

// True when sheng-adsp-stop asked to stop at this internal step (the step itself is done).
bool checkpoint(int value)
{
  stage(value);
  if (gStopAt != (unsigned)value) return false;
  IOLog("NativeAdsp: stopped after step %d as asked (sheng-adsp-stop)\n", value);
  return true;
}

// The command database lookups of the two rails: reads only (a copy held while the levels are looked up).
int lookupRails(uint32_t *lcx, uint32_t *lmx)
{
  uint8_t *db = static_cast<uint8_t *>(IOMallocData(kCmdDbSize));
  int rc = db ? 0 : -12;
  if (!rc && !readCmdDb(db)) rc = -12;
  if (!rc && (qcom_cmd_db_find(db, kCmdDbSize, "lcx.lvl", &gLcx) || qcom_cmd_db_find(db, kCmdDbSize, "lmx.lvl", &gLmx))) rc = -2;
  if (!rc && (qcom_rpmh_level_index(&gLcx, 0xffffffffu, lcx) || qcom_rpmh_level_index(&gLmx, 0xffffffffu, lmx))) rc = -2;
  if (db) IOFreeData(db, kCmdDbSize);
  return rc;
}

bool start()
{
  Image &dtb = gDtbImage, &adsp = gAdspImage;
  int rc;
  if (!carveoutsOutsideKernel()) {fail(StFirmware, -48); return false;}
  if ((!dtb.usable || !adsp.usable) && (rc = checkImages())) {fail(StFirmware, rc); return false;}
  native_adsp_stats[7] = (int32_t)(adsp.size >> 10);
  stage(StFirmware);
  if (native_adsp_stats[3] > 0) {fail(StFirmware, -114); return false;}  // running already: left alone
  if (gStopAt == StFirmware) return false;

  // Our SMP2P item, before the DSP starts (it looks for it at boot).
  if ((rc = allocItem(kItemOut, QCOM_SMP2P_ITEM_BYTES))) {fail(StSmp2p, rc); return false;}
  qcom_smp2p_init_out(&gP2p, reinterpret_cast<volatile uint32_t *>(item(kItemOut, nullptr)), 0, kRemotePid);
  gStopEntry = qcom_smp2p_add_entry(&gP2p, "master-kernel");
  if (checkpoint(StSmp2p)) return false;
  if (!attachIrqs()) return false;
  kickSmp2p();
  if (checkpoint(StDoorbells)) return false;

  qcom_qmp_init(&gQmp, reinterpret_cast<volatile uint32_t *>(gAoss.va), 0x400, kickAop, nullptr);
  for (int phase = 0; phase < 4 && qcom_qmp_step(&gQmp) > 0 && gQmp.state != QcomQmpOpen; ++phase)
    waitUntil(1000, [] {return qcom_qmp_step(&gQmp) == QcomQmpOpen || gQmp.state < 0;});
  native_adsp_stats[5] = (native_adsp_stats[5] & ~0xff) | (gQmp.state & 0xff);
  if (gQmp.state != QcomQmpOpen) {fail(StLoadState, gQmp.state); return false;}
  qcom_qmp_send(&gQmp, "{class: image, res: load_state, name: adsp, val: on}");
  if (!waitUntil(1000, [] {return qcom_qmp_step(&gQmp) == QcomQmpOpen;})) {
    qcom_qmp_abandon(&gQmp);
    fail(StLoadState, -60);
    return false;
  }
  if (checkpoint(StLoadState)) return false;

  uint32_t lcx = 0, lmx = 0;
  if ((rc = lookupRails(&lcx, &lmx))) {fail(StRails, rc); return false;}
  if ((rc = vote(lcx, lmx))) {fail(StRails, rc); return false;}
  if (checkpoint(StRails)) return false;

  // The DSP's devicetree image first, as its own authenticated image, then the DSP image.
  if ((rc = authenticateHeader(dtb, kPasDtb, kDtbBase)) || (rc = loadSegments(dtb, kDtbBase, kDtbSize)) ||
      (rc = scmOk(scm(QCOM_SCM_PAS_AUTH_AND_RESET, qcom_scm_args(1, 0, 0, 0), kPasDtb, 0, 0)) ? 0 : -3)) {
    fail(StDtb, rc); vote(0, 0); return false;
  }
  if (checkpoint(StDtb)) return false;
  if ((rc = authenticateHeader(adsp, kPasAdsp, kAdspBase)) || (rc = loadSegments(adsp, kAdspBase, kAdspSize))) {
    fail(StLoaded, rc); shutdownDsp(true, false); vote(0, 0); return false;
  }
  if (checkpoint(StLoaded)) return false;
  const uint64_t began = mach_absolute_time();
  if (!scmOk(scm(QCOM_SCM_PAS_AUTH_AND_RESET, qcom_scm_args(1, 0, 0, 0), kPasAdsp, 0, 0))) {
    fail(StStarted, -3); shutdownDsp(true, false); vote(0, 0); return false;
  }
  if (checkpoint(StStarted)) return false;
  const bool ready = waitUntil(5000, [] {return (gSlave & (kReady | kFatal)) != 0;});
  uint64_t ns = 0;
  absolutetime_to_nanoseconds(mach_absolute_time() - began, &ns);
  native_adsp_stats[8] = (int32_t)(ns / 1000000);
  releaseMeta(dtb);
  releaseMeta(adsp);  // the metadata may go once the images are authenticated
  if (!ready || (gSlave & kFatal)) {
    readCrashReason();
    fail(StReady, ready ? -5 : -60);
    shutdownDsp(true, true);
    if (!gHandedOver) vote(0, 0);
    return false;
  }
  stage(StReady);
  return gStopAt != StReady;
}

bool startGlink()
{
  int rc = allocItem(kItemGlinkDesc, 32);
  if (!rc) rc = allocItem(kItemGlinkTx, kGlinkFifo);
  uint32_t size = 0;
  volatile uint8_t *desc = item(kItemGlinkDesc, nullptr), *txf = item(kItemGlinkTx, &size);
  if (rc || !desc || !txf) {fail(StGlink, rc ? rc : -2); return false;}
  volatile uint32_t *d = reinterpret_cast<volatile uint32_t *>(desc);
  // Descriptor words: our FIFO's tail and head, then the DSP's FIFO's tail and head.
  const QcomGlinkFifo tx = {&d[1], &d[0], txf, size}, rx = {&d[3], &d[2], nullptr, 0};
  qcom_glink_init(&gGlink, tx, rx, kickGlink, nullptr);
  gGlinkStarted = true;
  applyClients();
  serviceGlink();
  return true;
}

void adspThread(void *, wait_result_t)
{
  thread_set_thread_name(current_thread(), "sheng-adsp");
  if (!mapWindow(gSmemW, kSmem, kSmemSize) || !mapWindow(gMutex, kMutex, 0x1000) || !mapWindow(gIpcc, kIpcc, 0x1000) ||
      !mapWindow(gAoss, kAoss, 0x1000) || !mapWindow(gRsc, kRscDrv, 0x10000)) {
    fail(StMapped, -12);
    return;
  }
  gSmem = {gSmemW.va, (uint32_t)kSmemSize};
  gLayout = qcom_rsc_layout(rd(gRsc, 0));
  stage(StMapped);
  survey();
  stage(StSurveyed);
  if (gMode >= 2) {
    uint32_t lcx = 0, lmx = 0;
    const ScmResult adsp = scm(QCOM_SCM_PAS_IS_SUPPORTED, qcom_scm_args(1, 0, 0, 0), kPasAdsp, 0, 0);
    const ScmResult dtb = scm(QCOM_SCM_PAS_IS_SUPPORTED, qcom_scm_args(1, 0, 0, 0), kPasDtb, 0, 0);
    const int images = checkImages(), rails = lookupRails(&lcx, &lmx);
    const uint32_t adspHash = gNodeData[1] ? sheng_adsp::digestPrefix(static_cast<const uint8_t *>(gNodeData[1]->getBytesNoCopy()), gNodeData[1]->getLength()) : 0;
    const uint32_t dtbHash = gNodeData[3] ? sheng_adsp::digestPrefix(static_cast<const uint8_t *>(gNodeData[3]->getBytesNoCopy()), gNodeData[3]->getLength()) : 0;
    IOLog("NativeAdsp: PAS supported adsp x0=%lld x1=%llu dtb x0=%lld x1=%llu; images rc=%d adsp %llu B %08x dtb %llu B %08x; rails rc=%d lcx %u lmx %u; "
          "carveouts %s\n", (long long)adsp.x0, (unsigned long long)adsp.x1, (long long)dtb.x0, (unsigned long long)dtb.x1, images,
          (unsigned long long)gAdspImage.size, adspHash, (unsigned long long)gDtbImage.size, dtbHash, rails, lcx, lmx,
          carveoutsOutsideKernel() ? "outside the kernel window" : "OVERLAP the kernel window");
  }
  if (gMode < 3) return;
  if (gDelay) IOSleep(gDelay * 1000);
  if (!start() || !startGlink()) return;
  // From here on the thread only answers doorbells and client sends; with nothing pending it sleeps without a deadline, except while
  // a send waits for an intent or FIFO room (then it looks again every second).
  for (;;) {
    bool pending = false;
    for (unsigned i = 0; i < gClientCount; ++i) pending |= gClients[i].count != 0;
    service(waitEvents(pending ? 1000 : 3600u * 1000u) | (pending ? kEvSend : 0));
  }
}

// Prints the state on the kernel console every 5 s for the first 2 minutes and on every change for 30 minutes, so a photo of the boot
// text records it. Reads only.
void adspReportThread(void *, wait_result_t)
{
  int32_t last[16] = {}, lastQrtr[16] = {};
  char text[sizeof native_adsp_text] = {}, row[sizeof native_adsp_text];
  for (unsigned second = 0; second <= 1800; ++second) {
    int32_t now[16], qrtr[16];
    bool changed = false, qrtrChanged = false;
    for (unsigned i = 0; i < 16; ++i) {
      now[i] = native_adsp_stats[i]; changed |= now[i] != last[i];
      qrtr[i] = native_qrtr_stats[i]; qrtrChanged |= qrtr[i] != lastQrtr[i];
    }
    if (changed || (second < 120 && second % 5 == 0)) {
      IOLog("NativeAdsp t%us: ADSP st%d e%d irq%d slave%x items%x qmp%x rsc%x fw%dK ready%dms gl%d/%d ch%x uo%d scm%x/%x votes%x\n", second, now[0], now[1], now[2],
            (uint32_t)now[3], (uint32_t)now[4], (uint32_t)now[5], (uint32_t)now[6], now[7], now[8], now[9], now[10], (uint32_t)now[11], now[12],
            (uint32_t)now[13], (uint32_t)now[14], (uint32_t)now[15]);
      memcpy(last, now, sizeof last);
    }
    if (qrtrChanged) {
      IOLog("NativeAdsp t%us: QRTR st%d in%d data%d bad%d out%d refused%d loc%d/%d node%d services%d\n", second, qrtr[0], qrtr[1], qrtr[2], qrtr[3], qrtr[4], qrtr[5],
            qrtr[6], qrtr[7], qrtr[8], qrtr[9]);
      memcpy(lastQrtr, qrtr, sizeof lastQrtr);
    }
    memcpy(row, native_adsp_text, sizeof row);
    row[sizeof row - 1] = 0;
    if (strncmp(row, text, sizeof row)) {
      IOLog("NativeAdsp text: %s\n", row);
      memcpy(text, row, sizeof text);
    }
    IOSleep(1000);
  }
}
}  // namespace

// ---- the client interface (ShengAdsp.h) ----
extern "C" ShengAdspChannel *sheng_adsp_channel(const char *name, uint32_t intent_size, uint32_t intent_count, int initiator,
                                                const ShengAdspClient *client, void *context)
{
  if (!gClientLock || !name || !client || !intent_size || !intent_count || intent_count > QCOM_GLINK_MAX_LOCAL_INTENTS) return nullptr;
  uint8_t *storage = static_cast<uint8_t *>(IOMallocData((vm_size_t)intent_size * intent_count));
  ShengAdspMessage *queue = static_cast<ShengAdspMessage *>(IOMallocData(sizeof(ShengAdspMessage) * kQueue));
  IOLockLock(gClientLock);
  ShengAdspChannel *c = gClientCount < kClients && storage && queue ? &gClients[gClientCount] : nullptr;
  if (c) {
    bzero(c, sizeof *c);
    strlcpy(c->name, name, sizeof c->name);
    c->intent_size = intent_size; c->intent_count = intent_count; c->initiator = initiator;
    c->client = *client; c->context = context; c->storage = storage; c->queue = queue;
    __atomic_store_n(&gClientCount, gClientCount + 1, __ATOMIC_RELEASE);
  }
  IOLockUnlock(gClientLock);
  if (!c) {
    if (storage) IOFreeData(storage, (vm_size_t)intent_size * intent_count);
    if (queue) IOFreeData(queue, sizeof(ShengAdspMessage) * kQueue);
    return nullptr;
  }
  wake(kEvSend);
  return c;
}

extern "C" int sheng_adsp_send(ShengAdspChannel *c, const void *data, uint32_t length)
{
  if (!c || !data || !length) return -2;
  if (length > SHENG_ADSP_MAX_MESSAGE) return -1;
  IOLockLock(gClientLock);
  if (c->count == kQueue) {IOLockUnlock(gClientLock); return -1;}
  ShengAdspMessage &m = c->queue[(c->head + c->count) % kQueue];
  memcpy(m.data, data, length);
  m.length = length;
  c->count++;
  IOLockUnlock(gClientLock);
  wake(kEvSend);
  return 0;
}

class NativeAdsp : public IOService {
  OSDeclareDefaultStructors(NativeAdsp)

public:
  bool start(IOService *provider) override;
};

OSDefineMetaClassAndStructors(NativeAdsp, IOService)

bool NativeAdsp::start(IOService *provider)
{
  uint32_t mode = 0;
  if (!PE_parse_boot_argn("sheng-adsp", &mode, sizeof(mode)) || mode < 1 || mode > 3)
    return false;
  if (PE_vmm_present != 0) {
    IOLog("NativeAdsp: the audio DSP is not driven in a VM\n");
    return false;
  }
  if (!IOService::start(provider))
    return false;
  gMode = mode;
  uint32_t value = 0;
  if (PE_parse_boot_argn("sheng-adsp-stop", &value, sizeof(value))) gStopAt = value;
  if (PE_parse_boot_argn("sheng-adsp-delay", &value, sizeof(value)) && value <= 600) gDelay = value;
  gClientLock = IOLockAlloc();
  const bool firmware = findFirmwareNode();
  IOLog("NativeAdsp: stage %u, firmware node %s (adsp %llu B, dtb %llu B)\n", mode, firmware ? "present" : "absent",
        (unsigned long long)gAdspImage.size, (unsigned long long)gDtbImage.size);
  if (!gClientLock) {
    fail(StMapped, -12);
    return false;
  }
  if (mode >= 3) {
    sheng_qrtr_start();  // the router link registers before the DSP can open it
    uint32_t battery = 0;
    if (PE_parse_boot_argn("sheng-battery", &battery, sizeof(battery)) && battery >= 1 && battery <= 2 && !native_battery_start(battery))
      IOLog("NativeAdsp: the battery service did not start\n");
  }
  thread_t thread = nullptr;
  if (kernel_thread_start(adspThread, nullptr, &thread) != KERN_SUCCESS) {
    fail(StMapped, -7);
    return false;
  }
  thread_deallocate(thread);
  if (kernel_thread_start(adspReportThread, nullptr, &thread) == KERN_SUCCESS)
    thread_deallocate(thread);
  registerService();
  return true;
}
