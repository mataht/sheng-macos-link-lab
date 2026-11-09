// SPDX-License-Identifier: GPL-3.0-or-later
// Host stand-ins for the kernel interfaces of platform/sheng/macos26/input/native_keyboard.cpp. The test maps every "physical" address to itself.
#pragma once
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <string>
#include <vector>

typedef int IOReturn;
typedef int wait_result_t;
typedef void *thread_t;
typedef int kern_return_t;
enum { kIOReturnSuccess = 0, kIOReturnNoMemory = 1, KERN_SUCCESS = 0 };
enum { kIODirectionNone = 0 };
enum { kIOMapAnywhere = 1, kIOMapInhibitCache = 2 };
enum { kHIDPage_GenericDesktop = 1, kHIDUsage_GD_Keyboard = 6 };
enum { kMillisecondScale = 1000000 };

void IOLog(const char *format, ...) __attribute__((format(printf, 1, 2)));
void IOSleep(unsigned ms);
void IODelay(unsigned us);
inline void *IOMalloc(size_t bytes) { return malloc(bytes); }
inline void IOFree(void *memory, size_t) { free(memory); }
uint64_t mach_absolute_time();
inline void absolutetime_to_nanoseconds(uint64_t absolute, uint64_t *ns) { *ns = absolute; }
inline void clock_interval_to_deadline(uint32_t interval, uint32_t scale, uint64_t *deadline) { *deadline = mach_absolute_time() + uint64_t(interval) * scale; }

struct OSString { static OSString *withCString(const char *) { return nullptr; } };
struct OSNumber { static OSNumber *withNumber(unsigned long long, unsigned) { return nullptr; } };

#define OSDeclareDefaultStructors(name)
#define OSDefineMetaClassAndStructors(name, base)
#define OSTypeAlloc(type) (new type)
#define OSSafeReleaseNULL(x) do { if (x) (x)->release(); (x) = nullptr; } while (0)

class OSObject {
public:
  virtual ~OSObject() = default;
};

class OSData : public OSObject {
public:
  std::vector<uint8_t> value;
  unsigned getLength() const { return static_cast<unsigned>(value.size()); }
  const void *getBytesNoCopy() const { return value.data(); }
};
#define OSDynamicCast(type, object) (dynamic_cast<type *>(object))

extern const void *gIODTPlane;
class IORegistryEntry {
public:
  OSData *property = nullptr;
  static IORegistryEntry *fromPath(const char *path, const void *plane);
  OSObject *getProperty(const char *) { return property; }
  void release() {}
};

class IOMemoryMap {
public:
  uint64_t address = 0;
  uint64_t getVirtualAddress() const { return address; }
};

class IOMemoryDescriptor {
public:
  std::vector<uint8_t> bytes;
  virtual ~IOMemoryDescriptor() = default;
  void release() { delete this; }
};

class IOBufferMemoryDescriptor : public IOMemoryDescriptor {
public:
  static IOBufferMemoryDescriptor *withBytes(const void *data, size_t size, int)
  {
    auto *d = new IOBufferMemoryDescriptor;
    d->bytes.assign(static_cast<const uint8_t *>(data), static_cast<const uint8_t *>(data) + size);
    return d;
  }
};

class IODeviceMemory : public IOMemoryDescriptor {
public:
  uint64_t physical = 0;
  static IODeviceMemory *withRange(uint64_t physical, uint64_t)
  {
    auto *m = new IODeviceMemory;
    m->physical = physical;
    return m;
  }
  IOMemoryMap *map(int)
  {
    auto *m = new IOMemoryMap;
    m->address = physical;
    return m;
  }
};

class IOService : public OSObject {
public:
  virtual bool start(IOService *) { return true; }
  bool init(void *) { return true; }
  bool attach(IOService *) { return true; }
  void detach(IOService *) {}
  void retain() {}
  void release() {}
  void registerService() {}
};

class IOHIDDevice : public IOService {
public:
  char pad[200];
  virtual IOReturn newReportDescriptor(IOMemoryDescriptor **descriptor) const = 0;
  virtual OSString *newTransportString() const { return nullptr; }
  virtual OSString *newManufacturerString() const { return nullptr; }
  virtual OSString *newProductString() const { return nullptr; }
  virtual OSNumber *newVendorIDNumber() const { return nullptr; }
  virtual OSNumber *newProductIDNumber() const { return nullptr; }
  virtual OSNumber *newLocationIDNumber() const { return nullptr; }
  virtual OSNumber *newPrimaryUsagePageNumber() const { return nullptr; }
  virtual OSNumber *newPrimaryUsageNumber() const { return nullptr; }
  IOReturn handleReport(IOMemoryDescriptor *report);
};

kern_return_t kernel_thread_start(void (*continuation)(void *, wait_result_t), void *argument, thread_t *thread);
inline void thread_deallocate(thread_t) {}
bool PE_parse_boot_argn(const char *name, void *destination, int size);
extern int PE_vmm_present;
