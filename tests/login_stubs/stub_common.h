// SPDX-License-Identifier: GPL-3.0-or-later
// Minimal declarations so the login services source can be type-checked on the host (no Apple headers).
#pragma once
#include <stddef.h>
#include <stdint.h>
#include <string.h>
typedef int IOReturn;
typedef uint32_t UInt32;
typedef void *task_t;
#define kIOReturnSuccess 0
#define kIOReturnError ((IOReturn)0xe00002bc)
#define kIOReturnNoMemory ((IOReturn)0xe00002bd)
#define kIOReturnNoSpace ((IOReturn)0xe00002c4)
#define kIOReturnBadArgument ((IOReturn)0xe00002c2)
#define kIOReturnUnsupported ((IOReturn)0xe00002c7)
class OSMetaClass;
class OSDictionary;
class OSObject {
public:
  virtual ~OSObject() {}
  virtual void release() {}
};
#define OSDeclareDefaultStructors(cls) public: static OSMetaClass *metaClass; private:
#define OSDefineMetaClassAndStructors(cls, base) OSMetaClass *cls::metaClass = nullptr;
#define OSTypeAlloc(cls) (new cls)
#define OSSafeReleaseNULL(x) do { if (x) { (x)->release(); (x) = nullptr; } } while (0)
template <class T> T *osDynamicCastHelper(OSObject *o) { return dynamic_cast<T *>(o); }
#define OSDynamicCast(cls, value) osDynamicCastHelper<cls>(value)
class IORegistryEntry : public OSObject {
public:
  OSObject *copyProperty(const char *) { return nullptr; }
  bool setProperty(const char *, bool) { return true; }
  bool setProperty(const char *, unsigned long long, unsigned int) { return true; }
};
class IOPlatformExpert;
class IOService : public IORegistryEntry {
public:
  virtual bool start(IOService *) { return true; }
  virtual IOReturn newUserClient(task_t, void *, UInt32, OSDictionary *, class IOUserClient **) { return 0; }
  virtual IOReturn newUserClient(task_t, void *, UInt32, class IOUserClient **) { return 0; }
  void registerService() {}
  bool attach(IOService *) { return true; }
  void detach(IOService *) {}
  bool terminate() { return true; }
  IOService *getProvider() { return nullptr; }
  static IOPlatformExpert *getPlatform() { return nullptr; }
};
class IOPlatformExpert : public IOService {};
class IOMemoryDescriptor : public OSObject {
public:
  size_t getLength() { return 0; }
  void writeBytes(size_t, const void *, size_t) {}
};
struct IOExternalMethodArguments {
  const uint64_t *scalarInput; uint32_t scalarInputCount;
  uint64_t *scalarOutput; uint32_t scalarOutputCount;
  const void *structureInput; uint32_t structureInputSize;
  void *structureOutput; uint32_t structureOutputSize;
  IOMemoryDescriptor *structureOutputDescriptor; uint32_t structureOutputDescriptorSize;
};
struct IOExternalMethodDispatch {};
class IOUserClient : public IOService {
public:
  virtual bool initWithTask(task_t, void *, UInt32, OSDictionary *) { return true; }
  virtual IOReturn clientClose() { return 0; }
  virtual IOReturn externalMethod(uint32_t, IOExternalMethodArguments *, IOExternalMethodDispatch *, OSObject *, void *) { return 0; }
};
class OSString : public OSObject { public: const char *getCStringNoCopy() { return nullptr; } };
class OSData : public OSObject { public: unsigned getLength() { return 0; } const void *getBytesNoCopy() { return nullptr; } };
#define kIOPlatformUUIDKey "IOPlatformUUID"
extern "C" void IOLog(const char *, ...) __attribute__((format(printf, 1, 2)));
extern "C" bool PE_parse_boot_argn(const char *, void *, int);
