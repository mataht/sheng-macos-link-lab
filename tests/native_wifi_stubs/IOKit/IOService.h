// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "IOKit/IOMemoryDescriptor.h"
class IOService : public OSObj {
public:
    virtual bool start(IOService *) { return true; }
    void registerService() {}
    void retain() {}
};
#define OSDeclareDefaultStructors(x)
#define OSDefineMetaClassAndStructors(x, y)
