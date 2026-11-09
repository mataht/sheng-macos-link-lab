#pragma once
#include <IOKit/IOService.h>
class OSData : public OSObject {
public:
  const void *getBytesNoCopy() const { return nullptr; }
  unsigned getLength() const { return 0; }
};
