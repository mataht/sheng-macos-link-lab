// SPDX-License-Identifier: GPL-3.0-or-later
// A minimal host model of the kernel interfaces native_adsp.cpp and native_battery.cpp use, so their real sources compile with -Werror
// and run on a host against a simulated DSP (tests/native_adsp_host.cpp). Threads are pthreads, the work loop is one pthread. This says
// nothing about the real kernel; only the real SDK compile in CI and a hardware boot can.
#pragma once
#include <pthread.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <time.h>
#include <unistd.h>

typedef int IOReturn;
typedef int kern_return_t;
typedef uint32_t IOOptionBits;
typedef uint64_t IOByteCount;
typedef size_t vm_size_t;
typedef void *event_t;
typedef int wait_result_t;
typedef void *task_t;
typedef void (*thread_continue_t)(void *, wait_result_t);
typedef struct HostThread *thread_t;
#define KERN_SUCCESS 0
#define kIOReturnSuccess 0
#define kIOReturnNotReady ((int)0xe00002d8)
#define THREAD_UNINT 0
#define THREAD_AWAKENED 0
#define THREAD_CONTINUE_NULL ((thread_continue_t)0)
#define kMillisecondScale 1000000u
constexpr IOOptionBits kIOMapAnywhere = 1, kIOMapInhibitCache = 0x100, kIOMapWriteCombineCache = 0x400, kIODirectionInOut = 3,
                       kIOMemoryPhysicallyContiguous = 0x10, kIOMemoryMapperNone = 0x800;
extern task_t kernel_task;
extern const char *gIODTPlane;
extern unsigned PE_vmm_present;

static inline size_t host_strlcpy(char *to, const char *from, size_t size)
{
  size_t n = strlen(from);
  if (size) {size_t c = n >= size ? size - 1 : n; memcpy(to, from, c); to[c] = 0;}
  return n;
}
#define strlcpy host_strlcpy

extern "C" {
void IOLog(const char *format, ...) __attribute__((format(printf, 1, 2)));
void IOSleep(unsigned ms);
void IODelay(unsigned us);
void *IOMallocData(vm_size_t size);
void IOFreeData(void *pointer, vm_size_t size);
bool PE_parse_boot_argn(const char *name, void *to, int size);
kern_return_t kernel_thread_start(thread_continue_t fn, void *arg, thread_t *out);
void thread_deallocate(thread_t t);
thread_t current_thread(void);
void thread_set_thread_name(thread_t t, const char *name);
void host_wakeup(event_t e);
wait_result_t assert_wait_timeout(event_t e, int interruptible, uint32_t interval, uint32_t scale);
void clear_wait(thread_t t, int result);
void thread_block(thread_continue_t c);
void clock_interval_to_deadline(uint32_t interval, uint32_t scale, uint64_t *deadline);
uint64_t mach_absolute_time(void);
void absolutetime_to_nanoseconds(uint64_t absolute, uint64_t *nanos);
}
#define thread_wakeup(e) host_wakeup(e)

struct HostLock {pthread_mutex_t m;};
typedef HostLock IOLock;
typedef HostLock IOSimpleLock;
inline HostLock *IOLockAlloc() {HostLock *l = new HostLock; pthread_mutex_init(&l->m, nullptr); return l;}
inline void IOLockLock(HostLock *l) {pthread_mutex_lock(&l->m);}
inline void IOLockUnlock(HostLock *l) {pthread_mutex_unlock(&l->m);}
inline HostLock *IOSimpleLockAlloc() {return IOLockAlloc();}
inline void IOSimpleLockLock(HostLock *l) {pthread_mutex_lock(&l->m);}
inline void IOSimpleLockUnlock(HostLock *l) {pthread_mutex_unlock(&l->m);}

class OSMetaClassBase {};
class OSObject : public OSMetaClassBase {
public:
  virtual ~OSObject() {}
  mutable int references = 1;
  virtual void retain() const {references++;}
  virtual void release() const {if (--references == 0) delete this;}
};
#define OSDynamicCast(Type, object) (dynamic_cast<Type *>((OSObject *)(object)))
#define OSSafeReleaseNULL(p) do {if (p) (p)->release(); (p) = nullptr;} while (0)
#define OSTypeAlloc(Type) (new Type)
#define OSDeclareDefaultStructors(Class)
#define OSDefineMetaClassAndStructors(Class, Base)

class OSData : public OSObject {
public:
  const void *bytes; unsigned length;
  OSData(const void *b, unsigned n) : bytes(b), length(n) {}
  const void *getBytesNoCopy() const {return bytes;}
  unsigned getLength() const {return length;}
};
class OSNumber : public OSObject {
public:
  unsigned long long value;
  explicit OSNumber(unsigned long long v) : value(v) {}
  static OSNumber *withNumber(unsigned long long v, int) {return new OSNumber(v);}
};
class OSSymbol : public OSObject {
public:
  char text[64];
  static const OSSymbol *withCString(const char *s) {OSSymbol *o = new OSSymbol; host_strlcpy(o->text, s, sizeof o->text); return o;}
};
class OSBoolean : public OSObject {};
extern OSBoolean *kOSBooleanTrue, *kOSBooleanFalse;
class OSDictionary : public OSObject {
public:
  static OSDictionary *withCapacity(unsigned) {return new OSDictionary;}
  bool setObject(const char *, OSObject *) {return true;}
};

class IORegistryEntry : public OSObject {
public:
  static IORegistryEntry *fromPath(const char *path, const char *plane);
  virtual OSObject *getProperty(const char *key);
};

class IOService : public IORegistryEntry {
public:
  virtual bool init() {return true;}
  virtual bool start(IOService *) {return true;}
  virtual bool attach(IOService *) {retain(); return true;}  // the real registry attachment retains the child
  virtual void detach(IOService *) {}
  virtual void registerService();
  static IOService *getPlatform();
};

class IOMemoryMap : public OSObject {
public:
  uint64_t base, size;
  uint8_t *va;
  void release() const override;
  uint64_t getVirtualAddress() {return (uint64_t)va;}
};
class IOMemoryDescriptor : public OSObject {
public:
  virtual IOMemoryMap *map(IOOptionBits options);
  virtual uint64_t getPhysicalSegment(uint64_t, void *, IOOptionBits) {return 0;}
};
class IODeviceMemory : public IOMemoryDescriptor {
public:
  uint64_t base, size;
  static IODeviceMemory *withRange(uint64_t start, uint64_t length) {IODeviceMemory *m = new IODeviceMemory; m->base = start; m->size = length; return m;}
  IOMemoryMap *map(IOOptionBits options) override;
};
class IOBufferMemoryDescriptor : public IOMemoryDescriptor {
public:
  void *memory; uint64_t bytes;
  static IOBufferMemoryDescriptor *inTaskWithPhysicalMask(task_t, IOOptionBits, uint64_t capacity, uint64_t);
  IOReturn prepare() {return kIOReturnSuccess;}
  void *getBytesNoCopy() {return memory;}
  uint64_t getPhysicalSegment(uint64_t, void *, IOOptionBits) override {return 0x8c0000000ull;}
};

class IOWorkLoop;
class IOEventSource : public OSObject {
public:
  IOWorkLoop *loop = nullptr;
  OSObject *owner = nullptr;
  virtual void fire() = 0;
};
class IOTimerEventSource : public IOEventSource {
public:
  typedef void (*Action)(OSObject *, IOTimerEventSource *);
  Action action = nullptr;
  uint64_t due = 0;
  static IOTimerEventSource *timerEventSource(OSObject *o, Action a) {IOTimerEventSource *s = new IOTimerEventSource; s->owner = o; s->action = a; return s;}
  void fire() override {action(owner, this);}
  void setTimeoutMS(uint32_t ms);
  void cancelTimeout();
};
class IOInterruptEventSource : public IOEventSource {
public:
  typedef void (*Action)(OSObject *, IOInterruptEventSource *, int);
  Action action = nullptr;
  bool pending = false;
  static IOInterruptEventSource *interruptEventSource(OSObject *o, Action a) {IOInterruptEventSource *s = new IOInterruptEventSource; s->owner = o; s->action = a; return s;}
  void fire() override {action(owner, this, 0);}
  void interruptOccurred(void *, void *, int);
};
class IOWorkLoop : public OSObject {
public:
  pthread_mutex_t m;
  pthread_cond_t c;
  IOEventSource *sources[8];
  unsigned count = 0;
  static IOWorkLoop *workLoop();
  IOReturn addEventSource(IOEventSource *s);
  void *run();
};

#define kIOPMPSAdapterDetailsKey "AdapterDetails"
#define kIOPMPSAdapterDetailsFamilyKey "FamilyCode"
#define kIOPMPSAdapterDetailsWattsKey "Watts"
#define kIOPMPSAdapterDetailsVoltage "AdapterVoltage"
#define kIOPMPSAdapterDetailsAmperageKey "Amperage"

class IOPMPowerSource : public IOService {
public:
  char padding[0x138 - sizeof(IOService)];

public:
  // Recorded by the setters for the test to read back (static: the object must keep the real class's size).
  struct Values {bool installed, external, capable, charging, critical; unsigned current, max, voltage, cycles; int amperage, time; unsigned updates;};
  static inline Values v = {};
  void setBatteryInstalled(bool b) {v.installed = b;}
  void setExternalConnected(bool b) {v.external = b;}
  void setExternalChargeCapable(bool b) {v.capable = b;}
  void setIsCharging(bool b) {v.charging = b;}
  void setAtCriticalLevel(bool b) {v.critical = b;}
  void setCurrentCapacity(unsigned n) {v.current = n;}
  void setMaxCapacity(unsigned n) {v.max = n;}
  void setVoltage(unsigned n) {v.voltage = n;}
  void setAmperage(int n) {v.amperage = n;}
  void setCycleCount(unsigned n) {v.cycles = n;}
  void setTimeRemaining(int n) {v.time = n;}
  void setPSProperty(const OSSymbol *, OSObject *) {}
  virtual void updateStatus();
};
