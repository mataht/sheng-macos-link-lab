#pragma once
#include <IOKit/IOService.h>
class OSArray : public OSObject {
public:
  static OSArray *withObjects(const OSObject **, unsigned, unsigned = 0) { return nullptr; }
};
