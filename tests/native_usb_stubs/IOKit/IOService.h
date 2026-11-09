// Minimal stand-ins for the host syntax and mangling checks of the USB companion; not an IOKit implementation.
#pragma once
#include <IOKit/IOTypes.h>
typedef unsigned int UInt32;
typedef int SInt32;
typedef unsigned long IOPMPowerFlags;
typedef void (*IOInterruptAction)(class OSObject *, void *, class IOService *, int);
enum { kIOInterruptTypeLevel = 1 };
enum { kIOReturnNoInterrupt = -2, kIOReturnNoResources = -3, kOSReturnSuccess = 0, kOSKextReturnStartStopError = -4 };
class OSSymbol;
class OSSerialize;
class IOWorkLoop;
class OSData;
class OSDictionary;
class OSIterator;
class OSNumber;
class OSArray;
class OSObject {
public:
  virtual ~OSObject() {}
  virtual void release() const {}
  virtual const struct OSMetaClass *getMetaClass() const { return nullptr; }
protected:
  virtual void free() {}
};
struct OSMetaClass {};
class OSBoolean : public OSObject {};
extern OSBoolean *const kOSBooleanTrue;
#define OSDeclareAbstractStructors(name) \
  public: static OSMetaClass gMetaClass; const OSMetaClass *getMetaClass() const override; \
  protected: name(const OSMetaClass *); virtual ~name(); \
  private:
#define OSDeclareDefaultStructors(name) \
  public: static OSMetaClass gMetaClass; const OSMetaClass *getMetaClass() const override; name(); virtual ~name(); \
  protected: name(const OSMetaClass *); \
  private:
#define OSDefineMetaClassAndStructors(name, base) \
  OSMetaClass name::gMetaClass; const OSMetaClass *name::getMetaClass() const { return &gMetaClass; } \
  name::name() : base(&gMetaClass) {} name::name(const OSMetaClass *m) : base(m) {} name::~name() {}
#define OSSafeReleaseNULL(p) do { if (p) { (p)->release(); (p) = nullptr; } } while (0)
#define OSTypeAlloc(type) (new type)
#define OSDynamicCast(type, inst) (static_cast<type *>(inst))
class IOService;
class IORegistryPlane;
extern const IORegistryPlane *gIODTPlane;
class IORegistryEntry : public OSObject {
public:
  static IORegistryEntry *fromPath(const char *, const IORegistryPlane *) { return nullptr; }
  virtual OSObject *getProperty(const char *) const { return nullptr; }
  virtual bool setProperty(const char *, OSObject *) { return true; }
  virtual bool setProperty(const char *, void *, unsigned) { return true; }
  virtual void setName(const char *, void * = nullptr) {}
};
class IOService : public IORegistryEntry {
public:
  IOService() {}
  explicit IOService(const OSMetaClass *) {}
  virtual ~IOService() {}
  virtual bool init(OSDictionary * = nullptr) { return true; }
  virtual IOReturn setProperties(OSObject *) { return 0; }
  virtual bool didTerminate(IOService *, unsigned int, bool *) { return true; }
  virtual void systemWillShutdown(unsigned int) {}
  virtual void registerService(unsigned int = 0) {}
  virtual bool start(IOService *) { return true; }
  virtual void stop(IOService *) {}
  virtual bool open(IOService *, unsigned int = 0, void * = nullptr) { return true; }
  virtual void close(IOService *, unsigned int = 0) {}
  virtual bool handleOpen(IOService *, unsigned int, void *) { return true; }
  virtual void handleClose(IOService *, unsigned int) {}
  virtual bool handleIsOpen(const IOService *) const { return true; }
  virtual bool terminate(unsigned int = 0) { return true; }
  virtual bool matchPropertyTable(OSDictionary *, SInt32 *) { return true; }
  virtual bool attach(IOService *) { return true; }
  virtual void detach(IOService *) {}
  virtual IOWorkLoop *getWorkLoop() const { return nullptr; }
  virtual IOReturn message(UInt32, IOService *, void * = nullptr) { return 0; }
  virtual void PMstop() {}
  virtual IOReturn setPowerState(unsigned long, IOService *) { return 0; }
  virtual unsigned long maxCapabilityForDomainState(unsigned long) { return 0; }
  virtual unsigned long initialPowerStateForDomainState(unsigned long) { return 0; }
  virtual IOReturn powerStateWillChangeTo(unsigned long, unsigned long, IOService *) { return 0; }
  virtual IOReturn powerStateDidChangeTo(unsigned long, unsigned long, IOService *) { return 0; }
  virtual IOReturn getInterruptType(int, int *) { return 0; }
  virtual IOReturn registerInterrupt(int, OSObject *, IOInterruptAction, void * = nullptr) { return 0; }
  virtual IOReturn unregisterInterrupt(int) { return 0; }
  virtual IOReturn enableInterrupt(int) { return 0; }
  virtual IOReturn disableInterrupt(int) { return 0; }
  unsigned long getPowerState() { return 0; }
  void setDeviceMemory(OSArray *) {}
  static IOService *getPlatform() { return nullptr; }
  static void publishResource(const char *, OSObject * = nullptr) {}
  static OSDictionary *serviceMatching(const char *, OSDictionary * = nullptr) { return nullptr; }
  static OSIterator *getMatchingServices(OSDictionary *) { return nullptr; }
};
