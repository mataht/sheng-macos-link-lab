#pragma once
#include <IOKit/IOService.h>
class OSIterator : public OSObject {
public:
  OSObject *getNextObject() { return nullptr; }
};
