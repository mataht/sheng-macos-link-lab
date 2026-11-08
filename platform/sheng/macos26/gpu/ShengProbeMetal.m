// SPDX-License-Identifier: GPL-3.0-or-later
// ShengProbeMetal: empty Metal plugin for the QEMU trust-path probe. It renders nothing and registers no device.
//
// The original +[IOGPUMetalDevice registerAcceleratorService:] loads this bundle, requires the class to subclass
// IOGPUMetalDevice and calls -initWithAcceleratorPort:. The two messages below say how far a process got: the constructor
// proves dyld and AMFI accepted the code, the method proves the Metal discovery reached it. Returning nil registers no
// device, so a WindowServer without the software UI policy still aborts after its 30-second wait.
//
// The superclass's initializer is deliberately not called (it would open an IOGPU user client this probe does not have),
// and the object is deliberately leaked: -[IOGPUMetalDevice dealloc] tears down state that initializer never made.
#import <Foundation/Foundation.h>
#include <IOKit/IOKitLib.h>
#include <mach/mach.h>
#include <stdio.h>
#include <unistd.h>

#define SHENG_PROBE_OPEN_TYPE 0x4147   // 'AG', the type the real plugin will use

@interface IOGPUMetalDevice : NSObject
@end

@interface ShengProbeMetalDevice : IOGPUMetalDevice
@end

__attribute__((constructor)) static void shengProbeLoaded(void)
{
    fprintf(stderr, "ShengProbeMetal: code loaded in %s[%d]\n", getprogname(), getpid());
    fflush(stderr);
}

@implementation ShengProbeMetalDevice
- (id)initWithAcceleratorPort:(io_service_t)port
{
    io_connect_t connection = IO_OBJECT_NULL;
    kern_return_t opened = IOServiceOpen(port, mach_task_self(), SHENG_PROBE_OPEN_TYPE, &connection);
    fprintf(stderr, "ShengProbeMetal: initWithAcceleratorPort in %s[%d], IOServiceOpen 0x%x\n", getprogname(), getpid(),
            (unsigned)opened);
    fflush(stderr);
    if (opened == KERN_SUCCESS) {
        IOServiceClose(connection);
    }
    return nil;
}
@end
