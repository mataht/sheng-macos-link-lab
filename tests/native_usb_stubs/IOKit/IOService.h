#pragma once
#include <IOKit/IOTypes.h>
class OSObject { public: virtual ~OSObject() {} virtual void release() const {} };
class OSMetaClass {};
#define OSDeclareDefaultStructors(name) public: name(); virtual ~name(); static OSMetaClass gMetaClass;
#define OSDefineMetaClassAndStructors(name, base) name::name() {} name::~name() {} OSMetaClass name::gMetaClass;
#define OSSafeReleaseNULL(p) do { if (p) { (p)->release(); (p) = nullptr; } } while (0)
class IOService : public OSObject {
public:
  virtual bool start(IOService *) { return true; }
  virtual void registerService(IOOptionBits = 0) {}
};
