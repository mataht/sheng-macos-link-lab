// SPDX-License-Identifier: GPL-3.0-or-later
// NativeGpuProbe: QEMU-only trust-path probe for the Metal plugin route. There is no GPU behind it.
//
// It answers one question: can code we built ourselves be loaded by the original WindowServer through the original Metal
// discovery? Metal finds IOAccelerator services, reads MetalPluginName and MetalPluginClassName from them, and loads
// /Library/GPUBundles/<name>.bundle (+[IOGPUMetalDevice registerAcceleratorService:] in 25F71).
//
// Default off (sheng-gpu-probe=1) and VM only (PE_vmm_present != 0); the hardware never links the VirtIO bundle. With the
// argument set, this class publishes the service, and a kernel thread waits for the Data volume to be mounted and writes
// the embedded plugin bundle there. In QEMU those writes land in the fixture's qcow2 overlay.
// A minimal user client lets the plugin test IOServiceOpen, which also exercises the sandbox rules for the service and
// its connection. It has no methods.
#include <IOKit/IOService.h>
#include <IOKit/IOUserClient.h>
#include <IOKit/IOLib.h>
#include <kern/thread.h>
#include <pexpert/pexpert.h>
#include <sys/errno.h>
#include <sys/fcntl.h>
#include <sys/mount.h>
#include <sys/uio.h>
#include <sys/vnode.h>
#include <sys/vnode_if.h>
#include "gpu_probe_payload.h"

extern "C" int sheng_kernel_mkdir(const char *path, int mode);   // platform/sheng/macos26/patches/0067
extern "C" void proc_selfname(char *buf, int size);
extern "C" int proc_selfpid(void);

// IOGraphicsFamily's class: no virtual method beyond IOService's, so its vtable is IOService's with its own structors.
// The storage-module audit compares every slot of the subclass below with the original IOAccelerator table (272 slots).
class IOAccelerator : public IOService {
    OSDeclareDefaultStructors(IOAccelerator)
};

class NativeGpuProbeUserClient : public IOUserClient {
    OSDeclareDefaultStructors(NativeGpuProbeUserClient)
public:
    IOReturn clientClose(void) override;
};
OSDefineMetaClassAndStructors(NativeGpuProbeUserClient, IOUserClient)

IOReturn NativeGpuProbeUserClient::clientClose(void)
{
    if (!isInactive()) {
        terminate();
    }
    return kIOReturnSuccess;
}

class NativeGpuProbeAccelerator : public IOAccelerator {
    OSDeclareDefaultStructors(NativeGpuProbeAccelerator)
public:
    using IOService::newUserClient;
    bool start(IOService *provider) override;
    IOReturn newUserClient(task_t owningTask, void *securityID, UInt32 type, OSDictionary *properties,
                           IOUserClient **handler) override;
};
OSDefineMetaClassAndStructors(NativeGpuProbeAccelerator, IOAccelerator)

#define PROBE_ROOT "/System/Volumes/Data/Library/GPUBundles"
#define PROBE_BUNDLE PROBE_ROOT "/" SHENG_GPU_PROBE_NAME ".bundle"

static int
probeWriteFile(const char *path, const unsigned char *data, size_t size, int mode, vfs_context_t context)
{
    vnode_t vp = NULLVP;
    int error = vnode_open(path, O_CREAT | O_TRUNC | FWRITE | O_NOFOLLOW, mode, 0, &vp, context);
    if (error) {
        return error;
    }
    int resid = 0;
    error = vn_rdwr(UIO_WRITE, vp, (caddr_t)(uintptr_t)data, (int)size, 0, UIO_SYSSPACE, IO_NOAUTH | IO_UNIT,
                    vfs_context_ucred(context), &resid, vfs_context_proc(context));
    if (!error && resid) {
        error = EIO;
    }
    if (!error) {
        error = VNOP_FSYNC(vp, MNT_WAIT, context);
    }
    int closed = vnode_close(vp, FWRITE, context);
    return error ? error : closed;
}

static int
probeMakeDirectory(const char *path)
{
    int error = sheng_kernel_mkdir(path, 0755);
    return error == EEXIST ? 0 : error;
}

static void
probeInstallThread(void *, wait_result_t)
{
    vfs_context_t context = vfs_context_kernel();
    vnode_t vp = NULLVP;
    // /System/Volumes/Data is an empty mount point until the boot tasks mount the Data volume.
    unsigned waited = 0;
    while (vnode_lookup("/System/Volumes/Data/Library", 0, &vp, context) != 0) {
        if (++waited > 3000) {
            IOLog("NativeGpuProbe: the Data volume did not appear in %u s; bundle not installed\n", waited);
            return;
        }
        IOSleep(1000);
    }
    vnode_put(vp);
    static const char *const directories[] = {PROBE_ROOT, PROBE_BUNDLE, PROBE_BUNDLE "/Contents",
                                              PROBE_BUNDLE "/Contents/MacOS"};
    for (const char *directory : directories) {
        int error = probeMakeDirectory(directory);
        if (error) {
            IOLog("NativeGpuProbe: mkdir %s failed: %d\n", directory, error);
            return;
        }
    }
    int plist = probeWriteFile(PROBE_BUNDLE "/Contents/Info.plist", gShengGpuProbeInfoPlist,
                               sizeof(gShengGpuProbeInfoPlist), 0644, context);
    int executable = probeWriteFile(PROBE_BUNDLE "/Contents/MacOS/" SHENG_GPU_PROBE_NAME, gShengGpuProbeExecutable,
                                    sizeof(gShengGpuProbeExecutable), 0755, context);
    IOLog("NativeGpuProbe: bundle install after %u s: Info.plist %d, executable %d (%lu bytes)\n", waited, plist,
          executable, (unsigned long)sizeof(gShengGpuProbeExecutable));
}

bool
NativeGpuProbeAccelerator::start(IOService *provider)
{
    uint32_t enabled = 0;
    if (!PE_parse_boot_argn("sheng-gpu-probe", &enabled, sizeof(enabled)) || enabled != 1) {
        return false;
    }
    if (PE_vmm_present == 0) {
        IOLog("NativeGpuProbe: refused outside a VM\n");
        return false;
    }
    if (!IOAccelerator::start(provider)) {
        return false;
    }
    thread_t thread = THREAD_NULL;
    if (kernel_thread_start(probeInstallThread, nullptr, &thread) == KERN_SUCCESS) {
        thread_deallocate(thread);
    } else {
        IOLog("NativeGpuProbe: could not start the bundle installer\n");
    }
    registerService();
    IOLog("NativeGpuProbe: IOAccelerator service published for plugin %s\n", SHENG_GPU_PROBE_NAME);
    return true;
}

IOReturn
NativeGpuProbeAccelerator::newUserClient(task_t owningTask, void *securityID, UInt32 type, OSDictionary *properties,
                                         IOUserClient **handler)
{
    char name[20] = {};
    proc_selfname(name, sizeof(name));
    IOLog("NativeGpuProbe: newUserClient from %s[%d] type 0x%x\n", name, proc_selfpid(), (unsigned)type);
    NativeGpuProbeUserClient *client = OSTypeAlloc(NativeGpuProbeUserClient);
    if (!client) {
        return kIOReturnNoMemory;
    }
    if (!client->initWithTask(owningTask, securityID, type, properties)) {
        client->release();
        return kIOReturnBadArgument;
    }
    if (!client->attach(this)) {
        client->release();
        return kIOReturnError;
    }
    if (!client->start(this)) {
        client->detach(this);
        client->release();
        return kIOReturnError;
    }
    *handler = client;
    return kIOReturnSuccess;
}
