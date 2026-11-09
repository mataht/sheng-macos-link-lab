// SPDX-License-Identifier: GPL-3.0-or-later
// Runs the real native_adsp.cpp and native_battery.cpp on a host, against a simulated SoC: SMEM, doorbells, RSC, secure calls and a
// simulated DSP built from the same upstream GLINK and SMP2P cores. Model test only. It checks that the stage arguments gate what they
// claim (stages 1 and 2 write nothing to any device window; the carveouts are never mapped before stage 3), that the bring-up sequence
// calls the secure world in the right order, and that battery values reach the IOPMPowerSource.
//
//   native_adsp_host STAGE BATTERY SCENARIO     SCENARIO: normal | overlap | nofirmware | stopped
#include <errno.h>
#include <sys/mman.h>
#include "iokit_stub.h"
extern "C" {
#include "QcomGlink.h"
#include "QcomMdt.h"
#include "QcomRpmh.h"
#include "QcomScm.h"
#include "QcomSmem.h"
#include "QcomSmp2p.h"
#include "ShengBattmgr.h"
extern volatile int32_t native_adsp_stats[16];
extern volatile int32_t native_battery_stats[16];
extern char native_adsp_text[96];
}
#include "adsp_sha256.h"

extern bool hostStartNativeAdsp();

task_t kernel_task;
const char *gIODTPlane = "IODeviceTree";
unsigned PE_vmm_present = 0;
OSBoolean *kOSBooleanTrue = new OSBoolean, *kOSBooleanFalse = new OSBoolean;
extern "C" {uint64_t gPhysBase = 0x8c0000000ull, gPhysSize = 0x300000000ull;}

static unsigned gChecks;
#define CHECK(x) do {if (!(x)) {fprintf(stderr, "%s:%d: %s\n", __FILE__, __LINE__, #x); _exit(1);} ++gChecks;} while (0)

// ---- memory and threads ----
static uint64_t nowNs() {struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return (uint64_t)t.tv_sec * 1000000000ull + (uint64_t)t.tv_nsec;}
extern "C" {
void IOLog(const char *format, ...) {if (getenv("NATIVE_ADSP_LOG")) {va_list a; va_start(a, format); vfprintf(stderr, format, a); va_end(a);}}
void IOSleep(unsigned ms) {struct timespec t = {ms / 1000, (long)(ms % 1000) * 1000000}; nanosleep(&t, nullptr);}
void IODelay(unsigned) {}
void *IOMallocData(vm_size_t size) {return calloc(1, size);}
void IOFreeData(void *p, vm_size_t) {free(p);}
uint64_t mach_absolute_time(void) {return nowNs();}
void absolutetime_to_nanoseconds(uint64_t a, uint64_t *n) {*n = a;}
void clock_interval_to_deadline(uint32_t i, uint32_t s, uint64_t *d) {*d = nowNs() + (uint64_t)i * s;}
void thread_deallocate(thread_t) {}
thread_t current_thread(void) {return (thread_t)pthread_self();}
void thread_set_thread_name(thread_t, const char *) {}
}
struct Start {thread_continue_t fn; void *arg;};
static void *trampoline(void *p) {Start s = *(Start *)p; delete (Start *)p; s.fn(s.arg, 0); return nullptr;}
extern "C" kern_return_t kernel_thread_start(thread_continue_t fn, void *arg, thread_t *out)
{
  pthread_t t;
  pthread_create(&t, nullptr, trampoline, new Start{fn, arg});
  *out = (thread_t)t;
  return KERN_SUCCESS;
}

// One waiter on one event at a time is all the driver does.
static pthread_mutex_t gWaitLock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t gWaitCond = PTHREAD_COND_INITIALIZER;
static bool gWoken;
static uint64_t gWaitDeadline;
extern "C" void host_wakeup(event_t) {pthread_mutex_lock(&gWaitLock); gWoken = true; pthread_cond_broadcast(&gWaitCond); pthread_mutex_unlock(&gWaitLock);}
extern "C" wait_result_t assert_wait_timeout(event_t, int, uint32_t interval, uint32_t scale) {pthread_mutex_lock(&gWaitLock); gWaitDeadline = nowNs() + (uint64_t)interval * scale; pthread_mutex_unlock(&gWaitLock); return 0;}
extern "C" void clear_wait(thread_t, int) {}
extern "C" void thread_block(thread_continue_t)
{
  pthread_mutex_lock(&gWaitLock);
  struct timespec until = {(time_t)(gWaitDeadline / 1000000000ull), (long)(gWaitDeadline % 1000000000ull)};
  (void)until;
  while (!gWoken && nowNs() < gWaitDeadline) {
    struct timespec t;
    clock_gettime(CLOCK_REALTIME, &t);
    t.tv_nsec += 2000000;
    if (t.tv_nsec >= 1000000000) {t.tv_sec++; t.tv_nsec -= 1000000000;}
    pthread_cond_timedwait(&gWaitCond, &gWaitLock, &t);
  }
  gWoken = false;
  pthread_mutex_unlock(&gWaitLock);
}

// ---- the work loop ----
IOWorkLoop *IOWorkLoop::workLoop()
{
  IOWorkLoop *l = new IOWorkLoop;
  pthread_mutex_init(&l->m, nullptr);
  pthread_cond_init(&l->c, nullptr);
  pthread_t t;
  pthread_create(&t, nullptr, [](void *p) -> void * {return ((IOWorkLoop *)p)->run();}, l);
  return l;
}
IOReturn IOWorkLoop::addEventSource(IOEventSource *s) {pthread_mutex_lock(&m); s->loop = this; sources[count++] = s; pthread_mutex_unlock(&m); return kIOReturnSuccess;}
void *IOWorkLoop::run()
{
  for (;;) {
    pthread_mutex_lock(&m);
    IOEventSource *due = nullptr;
    const uint64_t now = nowNs();
    for (unsigned i = 0; i < count && !due; ++i) {
      if (IOInterruptEventSource *e = dynamic_cast<IOInterruptEventSource *>(sources[i])) {if (e->pending) {e->pending = false; due = e;}}
      else if (IOTimerEventSource *t = dynamic_cast<IOTimerEventSource *>(sources[i])) {if (t->due && t->due <= now) {t->due = 0; due = t;}}
    }
    if (!due) {
      struct timespec t;
      clock_gettime(CLOCK_REALTIME, &t);
      t.tv_nsec += 1000000;
      if (t.tv_nsec >= 1000000000) {t.tv_sec++; t.tv_nsec -= 1000000000;}
      pthread_cond_timedwait(&c, &m, &t);
    }
    pthread_mutex_unlock(&m);
    if (due) due->fire();
  }
  return nullptr;
}
void IOTimerEventSource::setTimeoutMS(uint32_t ms) {pthread_mutex_lock(&loop->m); due = nowNs() + (uint64_t)ms * 1000000; pthread_cond_signal(&loop->c); pthread_mutex_unlock(&loop->m);}
void IOTimerEventSource::cancelTimeout() {pthread_mutex_lock(&loop->m); due = 0; pthread_mutex_unlock(&loop->m);}
void IOInterruptEventSource::interruptOccurred(void *, void *, int) {pthread_mutex_lock(&loop->m); pending = true; pthread_cond_signal(&loop->c); pthread_mutex_unlock(&loop->m);}

// ---- boot arguments and the firmware node ----
static unsigned gStageArg, gBatteryArg;
static int gStopArg;
extern "C" bool PE_parse_boot_argn(const char *name, void *to, int)
{
  const unsigned *value = nullptr;
  static const unsigned zero = 0;
  if (!strcmp(name, "sheng-adsp")) value = &gStageArg;
  else if (!strcmp(name, "sheng-adsp-delay")) value = &zero;
  else if (!strcmp(name, "sheng-battery") && gBatteryArg) value = &gBatteryArg;
  else if (!strcmp(name, "sheng-adsp-stop") && gStopArg) value = (const unsigned *)&gStopArg;
  if (value) memcpy(to, value, sizeof(unsigned));
  return value != nullptr;
}

static uint8_t gAdspFile[4096], gDtbFile[4096];
static uint8_t gAdspSum[32], gDtbSum[32];
static bool gHaveNode = true;
class Node : public IORegistryEntry {
public:
  OSObject *getProperty(const char *key) override
  {
    if (!strcmp(key, "firmware")) return new OSData(gAdspFile, sizeof gAdspFile);
    if (!strcmp(key, "firmware-dtb")) return new OSData(gDtbFile, sizeof gDtbFile);
    if (!strcmp(key, "firmware-sha256")) return new OSData(gAdspSum, 32);
    if (!strcmp(key, "firmware-dtb-sha256")) return new OSData(gDtbSum, 32);
    return nullptr;
  }
};
IORegistryEntry *IORegistryEntry::fromPath(const char *path, const char *) {return gHaveNode && !strcmp(path, "/arm-io/native-adsp") ? new Node : nullptr;}
OSObject *IORegistryEntry::getProperty(const char *) {return nullptr;}

static unsigned gRegistered, gStatusUpdates;
static IOPMPowerSource *gSource;
void IOService::registerService() {IOPMPowerSource *p = dynamic_cast<IOPMPowerSource *>(this); if (p) {gRegistered++; gSource = p;}}
IOService *IOService::getPlatform() {static IOService s; return &s;}
void IOPMPowerSource::updateStatus() {gStatusUpdates++; v.updates++;}
IOBufferMemoryDescriptor *IOBufferMemoryDescriptor::inTaskWithPhysicalMask(task_t, IOOptionBits, uint64_t capacity, uint64_t)
{
  IOBufferMemoryDescriptor *m = new IOBufferMemoryDescriptor;
  m->memory = calloc(1, capacity);
  m->bytes = capacity;
  return m;
}

// ---- the SoC model ----
static const uint64_t kSmem = 0x81d00000, kMutex = 0x1f43000, kIpcc = 0x408000, kAoss = 0xc300000, kRsc = 0x17a20000, kCmdDb = 0x81c60000;
static const uint64_t kAdsp = 0x9ea00000, kDtb = 0x9e980000;
struct Region {uint64_t base, size; uint8_t *host;} gRegions[8] = {
  {kSmem, 0x200000, nullptr}, {kMutex, 0x1000, nullptr}, {kIpcc, 0x1000, nullptr}, {kAoss, 0x1000, nullptr},
  {kRsc, 0x10000, nullptr}, {kCmdDb, 0x20000, nullptr}, {kAdsp, 0x4680000, nullptr}, {kDtb, 0x80000, nullptr},
};
static Region *regionOf(uint64_t base) {for (Region &r : gRegions) if (r.base == base) return &r; return nullptr;}
static Region *regionHost(const volatile void *a) {for (Region &r : gRegions) if ((const uint8_t *)a >= r.host && (const uint8_t *)a < r.host + r.size) return &r; return nullptr;}
static unsigned gMappedCarveouts, gWrites;
IOMemoryMap *IOMemoryDescriptor::map(IOOptionBits) {return nullptr;}
void IOMemoryMap::release() const {delete this;}
IOMemoryMap *IODeviceMemory::map(IOOptionBits)
{
  Region *r = regionOf(base);
  if (!r || r->size != size) {fprintf(stderr, "unexpected window %llx + %llx\n", (unsigned long long)base, (unsigned long long)size); _exit(1);}
  if (base == kAdsp || base == kDtb) __atomic_fetch_add(&gMappedCarveouts, 1, __ATOMIC_SEQ_CST);
  IOMemoryMap *m = new IOMemoryMap;
  m->base = base; m->size = size; m->va = r->host;
  return m;
}

static void (*gHandlers[1024])(void *);
extern "C" int armcpu_spi_attach(uint32_t intid, bool, void (*handler)(void *), void *, uint32_t *, uint64_t *) {gHandlers[intid] = handler; return 0;}

static pthread_mutex_t gBellLock = PTHREAD_MUTEX_INITIALIZER;
static uint32_t gToApps[32], gToAppsCount, gRings[32], gRingCount;
static void bellToApps(uint32_t id) {pthread_mutex_lock(&gBellLock); gToApps[gToAppsCount++] = id; pthread_mutex_unlock(&gBellLock);}

extern "C" uint32_t native_adsp_host_read(const volatile void *address)
{
  Region *r = regionHost(address);
  const uint64_t offset = (const uint8_t *)address - r->host;
  if (r->base == kIpcc && offset == QCOM_IPCC_RECV_ID) {
    pthread_mutex_lock(&gBellLock);
    uint32_t id = QCOM_IPCC_NONE;
    if (gToAppsCount) {id = gToApps[0]; memmove(gToApps, gToApps + 1, --gToAppsCount * sizeof gToApps[0]);}
    pthread_mutex_unlock(&gBellLock);
    return id;
  }
  return *(const volatile uint32_t *)address;
}
static QcomRscLayout gLayout;
extern "C" void native_adsp_host_write(volatile void *address, uint32_t value)
{
  __atomic_fetch_add(&gWrites, 1, __ATOMIC_SEQ_CST);
  Region *r = regionHost(address);
  const uint64_t offset = (const uint8_t *)address - r->host;
  if (r->base == kIpcc && offset == QCOM_IPCC_SEND) {pthread_mutex_lock(&gBellLock); gRings[gRingCount++] = value; pthread_mutex_unlock(&gBellLock); return;}
  *(volatile uint32_t *)address = value;
  if (r->base == kRsc && offset == 0xd00 + gLayout.tcs_stride + gLayout.control && (value & (1u << 24))) {
    *(volatile uint32_t *)(r->host + 0xd00 + gLayout.irq_status) = 2;
    if (gHandlers[32 + 5]) gHandlers[32 + 5](nullptr);
  }
}

// ---- secure calls ----
static pthread_mutex_t gScmLock = PTHREAD_MUTEX_INITIALIZER;
static struct {uint32_t command; uint64_t pas;} gScm[32];
static unsigned gScmCount;
static volatile bool gDspBooted;
extern "C" void native_adsp_host_smc(uint64_t r[8])
{
  CHECK((r[0] & 0xffffff00u) == 0x42000200u);
  pthread_mutex_lock(&gScmLock);
  if (gScmCount < 32) {gScm[gScmCount].command = (uint32_t)r[0] & 0xff; gScm[gScmCount].pas = r[2]; gScmCount++;}
  pthread_mutex_unlock(&gScmLock);
  const uint32_t command = (uint32_t)r[0] & 0xff;
  r[0] = 0;
  r[1] = command == QCOM_SCM_PAS_IS_SUPPORTED ? 1 : 0;
  if (command == QCOM_SCM_PAS_AUTH_AND_RESET && r[2] == 1) gDspBooted = true;
}

// ---- fixtures: SMEM, cmd-db, AOSS, RSC ----
static QcomSmem gSmem;
static QcomSmemPartition gPart;
static void w32(uint32_t at, uint32_t v) {*(volatile uint32_t *)(gRegions[0].host + at) = v;}
static void makePartition(uint32_t index, uint32_t offset, uint32_t size, uint16_t h0, uint16_t h1)
{
  const uint32_t e = 0x200000 - 0x1000 + 32 + 48 * index;
  w32(e, offset); w32(e + 4, size); w32(e + 8, 0); w32(e + 12, h0 | (uint32_t)h1 << 16); w32(e + 16, 64);
  w32(offset, 0x54525024u); w32(offset + 4, h0 | (uint32_t)h1 << 16); w32(offset + 8, size); w32(offset + 12, 32); w32(offset + 16, size);
}
static void put16(uint8_t *p, uint16_t v) {p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8);}
static void put32(uint8_t *p, uint32_t v) {put16(p, (uint16_t)v); put16(p + 2, (uint16_t)(v >> 16));}
static uint32_t le32(const uint8_t *p) {return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;}

static void buildFixtures()
{
  for (Region &r : gRegions) {
    r.host = (uint8_t *)mmap(nullptr, r.size, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    CHECK(r.host != MAP_FAILED);
  }
  const uint32_t toc = 0x200000 - 0x1000;
  w32(toc, 0x434f5424u); w32(toc + 4, 1); w32(toc + 8, 2);
  makePartition(0, 0x1000, 0x8000, QCOM_SMEM_HOST_GLOBAL, QCOM_SMEM_HOST_GLOBAL);
  makePartition(1, 0x10000, 0x40000, 2, 0);
  gSmem = {gRegions[0].host, 0x200000};
  CHECK(!qcom_smem_partition(&gSmem, 0, 2, &gPart));
  uint8_t *db = gRegions[5].host;
  put32(db, 1); put32(db + 4, QCOM_CMD_DB_MAGIC);
  put16(db + 8, 3); put16(db + 10, 0); put16(db + 12, 0x200); put16(db + 14, 2);
  const char *names[2] = {"lcx.lvl", "lmx.lvl"};
  for (unsigned i = 0; i < 2; ++i) {
    uint8_t *e = db + 144 + 24 * i;
    memcpy(e, names[i], strlen(names[i]));
    put32(e + 16, 0x30010 + 0x10 * i); put16(e + 20, 16); put16(e + 22, (uint16_t)(32 * i));
    static const uint16_t levels[8] = {0, 16, 48, 64, 128, 192, 256, 384};
    for (unsigned l = 0; l < 8; ++l) put16(db + 144 + 0x200 + 32 * i + 2 * l, levels[l]);
  }
  volatile uint32_t *ram = (volatile uint32_t *)gRegions[3].host;
  ram[0] = 0x4d41494cu; ram[1] = 1; ram[3] = 0xffff; ram[13] = 0x100; ram[14] = 0x100;
  *(volatile uint32_t *)gRegions[4].host = 0x00030000;  // RSC DRV id: major version 3
  gLayout = qcom_rsc_layout(0x00030000);
}

static size_t makeImage(uint8_t *f, uint32_t base)
{
  memset(f, 0, 4096);
  memcpy(f, "\x7f" "ELF", 4);
  f[4] = 1; f[5] = 1; f[6] = 1;
  put16(f + 16, 2); put16(f + 18, 164); put32(f + 28, 52); put16(f + 40, 52); put16(f + 42, 32); put16(f + 44, 4);
  const uint32_t hdr = 52 + 4 * 32;
  const struct {uint32_t type, off, paddr, filesz, memsz, flags;} ph[4] = {
    {0, 0, 0, hdr, 0, 7u << 24}, {0, 0x200, base, 0x40, 0x1000, 2u << 24},
    {1, 0x400, base + 0x100000, 0x100, 0x100, 0x5 | QCOM_MDT_RELOCATABLE}, {1, 0x600, base + 0x101000, 0x80, 0x200, 0x6 | QCOM_MDT_RELOCATABLE},
  };
  for (unsigned i = 0; i < 4; ++i) {
    uint8_t *p = f + 52 + 32 * i;
    put32(p, ph[i].type); put32(p + 4, ph[i].off); put32(p + 8, ph[i].paddr); put32(p + 12, ph[i].paddr);
    put32(p + 16, ph[i].filesz); put32(p + 20, ph[i].memsz); put32(p + 24, ph[i].flags); put32(p + 28, 0x1000);
  }
  for (unsigned i = 0; i < 0x100; ++i) f[0x400 + i] = (uint8_t)(i + 1);
  return 4096;
}

// ---- the simulated DSP ----
static QcomGlink gDsp;
static bool gDspGlink;
static QcomSmp2p gDspP2p;
static QcomGlinkChannel *gDspPmic;
static uint8_t gDspStore[4 * 1024], gDspRtr[8 * 1024];
static uint32_t gBatteryRequests, gLimitWrites;

static uint32_t reading(uint32_t opcode, uint32_t property)
{
  if (opcode == SHENG_BATTMGR_OP_BATTERY_GET) switch (property) {
    case 0: return 2; case 2: return 1; case 4: return 8700; case 7: return 3900000; case 9: return 500000; case 13: return 2850; case 15: return 8000000;
    case 16: return 172; case 17: return 9800000; case 18: return 9353000; case 21: return 18000; default: return 0;
  }
  if (opcode == SHENG_BATTMGR_OP_BOARD_GET) switch (property) {case 0x8a: return 4000; case 0x8b: return 4700; case 0xa6: return 4200; case 0xa7: return 4700; default: return 0;}
  return 0;
}
static void dspPmicReceive(void *, QcomGlinkChannel *c, const uint8_t *data, uint32_t length)
{
  CHECK(length >= 12 && le32(data) == SHENG_BATTMGR_OWNER);
  const uint32_t opcode = le32(data + 8);
  uint8_t reply[24];
  memcpy(reply, data, 12);
  if (opcode == SHENG_BATTMGR_OP_USB_SET) gLimitWrites++;
  if (opcode == SHENG_BATTMGR_OP_REQUEST_NOTIFICATION) {qcom_glink_send(&gDsp, c, reply, 12); return;}
  gBatteryRequests++;
  put32(reply + 12, le32(data + 16));
  put32(reply + 16, reading(opcode, le32(data + 16)));
  put32(reply + 20, 0);
  qcom_glink_send(&gDsp, c, reply, 24);
}
static void dspState(void *, QcomGlinkChannel *, int) {}
static void dspKick(void *) {bellToApps(qcom_ipcc_id(QCOM_IPCC_CLIENT_LPASS, QCOM_IPCC_SIGNAL_GLINK_QMP));}

static void aopWork()
{
  volatile uint32_t *ram = (volatile uint32_t *)gRegions[3].host;
  bool changed = false;
  if (ram[9] == 0xffff && ram[10] != 0xffff) {ram[10] = 0xffff; changed = true;}
  if (ram[11] == 0xffff && ram[5] != 0xffff) {ram[5] = 0xffff; changed = true;}
  if (ram[6] == 0xffff && ram[12] != 0xffff) {ram[12] = 0xffff; changed = true;}
  if (ram[0x100 / 4]) {ram[0x100 / 4] = 0; changed = true;}
  if (changed) bellToApps(qcom_ipcc_id(QCOM_IPCC_CLIENT_AOP, QCOM_IPCC_SIGNAL_GLINK_QMP));
}

static void dspWork()
{
  static bool booted;
  if (gDspBooted && !booted) {
    booted = true;
    uint32_t at = 0, size = 0;
    CHECK(!qcom_smem_alloc(&gSmem, &gPart, 443, QCOM_SMP2P_ITEM_BYTES));
    CHECK(!qcom_smem_find(&gSmem, &gPart, 443, &at, &size));
    qcom_smp2p_init_out(&gDspP2p, (volatile uint32_t *)(gRegions[0].host + at), 2, 0);
    const int entry = qcom_smp2p_add_entry(&gDspP2p, "slave-kernel");
    CHECK(!qcom_smem_alloc(&gSmem, &gPart, 480, 0x4000));
    qcom_smp2p_update(&gDspP2p, entry, 6, 6);  // ready and handover
    bellToApps(qcom_ipcc_id(QCOM_IPCC_CLIENT_LPASS, QCOM_IPCC_SIGNAL_SMP2P));
  }
  uint32_t descAt = 0, descSize = 0, txAt = 0, txSize = 0, rxAt = 0, rxSize = 0;
  if (booted && !gDspGlink && !qcom_smem_find(&gSmem, &gPart, 478, &descAt, &descSize) && !qcom_smem_find(&gSmem, &gPart, 479, &txAt, &txSize) &&
      !qcom_smem_find(&gSmem, &gPart, 480, &rxAt, &rxSize)) {
    volatile uint32_t *d = (volatile uint32_t *)(gRegions[0].host + descAt);
    const QcomGlinkFifo tx = {&d[3], &d[2], gRegions[0].host + rxAt, rxSize}, rx = {&d[1], &d[0], gRegions[0].host + txAt, txSize};
    qcom_glink_init(&gDsp, tx, rx, dspKick, nullptr);
    const QcomGlinkClient pmic = {dspState, dspPmicReceive}, router = {dspState, nullptr};
    gDspPmic = qcom_glink_channel(&gDsp, "PMIC_RTR_ADSP_APPS", 1024, 4, gDspStore, &pmic, nullptr, 1);
    CHECK(qcom_glink_channel(&gDsp, "IPCRTR", 1024, 8, gDspRtr, &router, nullptr, 1) != nullptr);
    gDspGlink = true;
  }
  if (gDspGlink) qcom_glink_poll(&gDsp);
}

static void *socThread(void *)
{
  for (;;) {
    aopWork();
    dspWork();
    pthread_mutex_lock(&gBellLock);
    const bool pending = gToAppsCount != 0;
    gRingCount = 0;
    pthread_mutex_unlock(&gBellLock);
    if (pending && gHandlers[32 + 229]) gHandlers[32 + 229](nullptr);
    IOSleep(1);
  }
  return nullptr;
}

static bool waitFor(bool (*done)(), unsigned ms) {for (unsigned i = 0; i < ms; ++i) {if (done()) return true; IOSleep(1);} return done();}
static bool stageReached13() {return native_adsp_stats[0] == 13;}
static bool batteryReached4() {return native_battery_stats[0] == 4 && native_battery_stats[6] >= 1;}
static bool anyFailure() {return native_adsp_stats[0] < 0;}
static bool surveyed() {return native_adsp_stats[0] == 2;}
static bool cycleDone() {return native_battery_stats[6] >= 2;}

int main(int argc, char **argv)
{
  setvbuf(stdout, nullptr, _IONBF, 0);
  CHECK(argc == 4);
  gStageArg = (unsigned)atoi(argv[1]);
  gBatteryArg = (unsigned)atoi(argv[2]);
  const char *scenario = argv[3];
  buildFixtures();
  makeImage(gAdspFile, 0x8a000000);
  makeImage(gDtbFile, 0x8b000000);
  CHECK(sheng_adsp::sha256(gAdspFile, sizeof gAdspFile, gAdspSum) && sheng_adsp::sha256(gDtbFile, sizeof gDtbFile, gDtbSum));
  if (!strcmp(scenario, "nofirmware")) gHaveNode = false;
  if (!strcmp(scenario, "overlap")) {gPhysBase = kAdsp; gPhysSize = 0x10000000;}
  if (!strcmp(scenario, "stopped")) gStopArg = 7;
  if (!strcmp(scenario, "tampered")) gAdspFile[0x500] ^= 1;
  if (gStageArg <= 2)
    for (Region &r : gRegions) CHECK(mprotect(r.host, r.size, PROT_READ) == 0);
  pthread_t soc;
  if (gStageArg == 3) pthread_create(&soc, nullptr, socThread, nullptr);
  CHECK(hostStartNativeAdsp());
  if (gStageArg <= 2) {
    CHECK(waitFor(surveyed, 2000) || gStageArg == 2);
    IOSleep(100);
    CHECK(gWrites == 0);              // no device write at stage 1 or 2 (the SMEM window is read-only here, so a core write would fault)
    CHECK(gMappedCarveouts == 0);
    CHECK(native_adsp_stats[0] == 2);
    CHECK(native_adsp_stats[4] == (1 << 8 | 1 << 9));  // both partitions found, no items before the DSP runs
    CHECK(native_adsp_stats[3] == -1);
    CHECK(gScmCount == (gStageArg == 2 ? 2u : 0u));
    if (gStageArg == 2) CHECK(gScm[0].command == QCOM_SCM_PAS_IS_SUPPORTED && gScm[0].pas == 1 && gScm[1].pas == 36);
    printf("PASS native adsp host stage %u: %u checks\n", gStageArg, gChecks);
    _exit(0);
  }
  if (!strcmp(scenario, "overlap") || !strcmp(scenario, "nofirmware") || !strcmp(scenario, "tampered")) {
    CHECK(waitFor(anyFailure, 2000));
    CHECK(native_adsp_stats[0] == -3);
    CHECK(native_adsp_stats[1] == (!strcmp(scenario, "overlap") ? -48 : !strcmp(scenario, "tampered") ? -80 : -2));
    CHECK(gMappedCarveouts == 0 && gScmCount == 2);  // only the two is-supported queries
    printf("PASS native adsp host %s: %u checks\n", scenario, gChecks);
    _exit(0);
  }
  if (!strcmp(scenario, "stopped")) {
    IOSleep(1500);
    CHECK(native_adsp_stats[0] == 7);
    CHECK(gMappedCarveouts == 0 && gScmCount == 2);
    printf("PASS native adsp host stopped: %u checks\n", gChecks);
    _exit(0);
  }
  CHECK(waitFor(stageReached13, 5000));
  CHECK(native_adsp_stats[3] == 6);
  // The secure calls, in order: dtb init, setup, reset; adsp init, setup, reset.
  static const uint32_t order[6][2] = {{QCOM_SCM_PAS_INIT_IMAGE, 36}, {QCOM_SCM_PAS_MEM_SETUP, 36}, {QCOM_SCM_PAS_AUTH_AND_RESET, 36},
                                       {QCOM_SCM_PAS_INIT_IMAGE, 1}, {QCOM_SCM_PAS_MEM_SETUP, 1}, {QCOM_SCM_PAS_AUTH_AND_RESET, 1}};
  CHECK(gScmCount == 8 && gScm[0].command == QCOM_SCM_PAS_IS_SUPPORTED && gScm[1].command == QCOM_SCM_PAS_IS_SUPPORTED);
  for (unsigned i = 0; i < 6; ++i) CHECK(gScm[i + 2].command == order[i][0] && gScm[i + 2].pas == order[i][1]);
  CHECK(gMappedCarveouts == 2);
  CHECK(gRegions[6].host[0x00] == 1 && gRegions[6].host[0xff] == 0 && gRegions[7].host[0x00] == 1);  // the first loaded bytes reached both carveouts
  // The vote went out, the DSP took over and the vote was released.
  CHECK((native_adsp_stats[15] & 0xff) == 7 || (native_adsp_stats[15] & 0xff) == 0);
  CHECK(gLimitWrites == 0);
  if (gBatteryArg) {
    CHECK(waitFor(batteryReached4, 4000));
    CHECK(waitFor(cycleDone, 200) || native_battery_stats[6] >= 1);
    CHECK(native_battery_stats[1] == 87);
    CHECK(native_battery_stats[2] == 3900);
    CHECK(native_battery_stats[3] == -500);
    CHECK((native_battery_stats[5] & 0xf) == 1);  // present, on battery, not charging
    CHECK(native_battery_stats[11] == 9353 && native_battery_stats[12] == 9800 && native_battery_stats[13] == 172);
    CHECK(gLimitWrites == 0);
    if (gBatteryArg == 1) {
      CHECK(gRegistered == 0 && gStatusUpdates == 0);
    } else {
      CHECK(gRegistered == 1 && gStatusUpdates >= 1 && gSource);
      CHECK(gSource->v.installed && !gSource->v.external && !gSource->v.charging && !gSource->v.critical);
      CHECK(gSource->v.current == 87 && gSource->v.max == 100 && gSource->v.voltage == 3900 && gSource->v.amperage == -500 && gSource->v.time == 300);
    }
  }
  printf("PASS native adsp host stage 3 battery %u: %u checks\n", gBatteryArg, gChecks);
  _exit(0);
}
