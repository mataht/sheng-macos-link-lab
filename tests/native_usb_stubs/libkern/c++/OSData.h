#pragma once
#include <IOKit/IOService.h>
class OSData : public OSObject {
public:
  static OSData *withBytes(const void *, unsigned) { return nullptr; }
  const void *getBytesNoCopy() const { return nullptr; }
  unsigned getLength() const { return 0; }
};
