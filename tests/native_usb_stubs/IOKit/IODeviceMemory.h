#pragma once
#include <IOKit/IOService.h>
class IODeviceMemory : public OSObject {
public:
  static IODeviceMemory *withRange(uint64_t, uint64_t) { return nullptr; }
};
