#pragma once
#include <IOKit/IOTypes.h>
class OSObject { public: virtual ~OSObject() {} virtual void release() const {} };
class OSMetaClass {};
#define OSDeclareDefaultStructors(name) public: name(); virtual ~name(); static OSMetaClass gMetaClass;
#define OSDefineMetaClassAndStructors(name, base) name::name() {} name::~name() {} OSMetaClass name::gMetaClass;
#define OSSafeReleaseNULL(p) do { if (p) { (p)->release(); (p) = nullptr; } } while (0)
class IORegistryPlane;
extern const IORegistryPlane *gIODTPlane;
#define OSDynamicCast(type, inst) (static_cast<type *>(inst))
class IORegistryEntry : public OSObject {
public:
  static IORegistryEntry *fromPath(const char *, const IORegistryPlane *) { return nullptr; }
  OSObject *getProperty(const char *) const { return nullptr; }
};
class IOService : public IORegistryEntry {
public:
  virtual bool start(IOService *) { return true; }
  virtual void registerService(IOOptionBits = 0) {}
};
