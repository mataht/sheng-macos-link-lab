#pragma once
#include <IOKit/IOService.h>
class IOMemoryMap : public OSObject {
public:
  mach_vm_address_t getVirtualAddress() { return 0; }
};
class IOMemoryDescriptor : public OSObject {
public:
  static IOMemoryDescriptor *withPhysicalAddress(uint64_t, uint64_t, IODirection) { return nullptr; }
  IOMemoryMap *createMappingInTask(task_t, mach_vm_address_t, IOOptionBits) { return nullptr; }
};
