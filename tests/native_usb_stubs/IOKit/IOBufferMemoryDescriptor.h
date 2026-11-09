#pragma once
#include <IOKit/IOMemoryDescriptor.h>
enum { kIOMemoryPhysicallyContiguous = 8 };
class IOBufferMemoryDescriptor : public IOMemoryDescriptor {
public:
  static IOBufferMemoryDescriptor *withOptions(IOOptionBits, size_t, size_t) { return nullptr; }
  void *getBytesNoCopy() { return nullptr; }
  IOReturn prepare() { return 0; }
  uint64_t getPhysicalSegment(uint64_t, uint64_t *, IOOptionBits) { return 0; }
};
