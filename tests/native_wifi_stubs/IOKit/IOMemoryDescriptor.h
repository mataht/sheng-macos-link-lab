// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <stdint.h>
struct task;
extern task *kernel_task;
typedef unsigned IOOptionBits;
enum { kIODirectionInOut = 3, kIOMapAnywhere = 1, kIOMapInhibitCache = 2 };
class OSObj {
public:
    virtual ~OSObj() = default;
    void release() { delete this; }
};
class IOMemoryMap : public OSObj {
public:
    uint64_t va = 0;
    uint64_t getVirtualAddress() const { return va; }
};
class IOMemoryDescriptor : public OSObj {
public:
    uint64_t physical = 0, bytes = 0;
    static IOMemoryDescriptor *withPhysicalAddress(uint64_t physical, uint64_t bytes, unsigned direction);
    IOMemoryMap *createMappingInTask(task *, uint64_t, IOOptionBits);
};
#define OSSafeReleaseNULL(p) do { if (p) { (p)->release(); (p) = nullptr; } } while (0)
