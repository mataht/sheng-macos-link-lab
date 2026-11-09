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
//
// A local test of the real plugin adds /arm-io/native-gpu-plugin to the device tree: the service then names that plugin and the installer
// writes the bundle from the node instead. A new plugin needs no new kernel, and the bundle stays out of every kernel build.
#include <IOKit/IOService.h>
#include <IOKit/IOUserClient.h>
#include <IOKit/IOLib.h>
#include <IOKit/IODeviceTreeSupport.h>
#include <IOKit/IOBufferMemoryDescriptor.h>
#include <kern/clock.h>
#include <libkern/c++/OSData.h>
#include <libkern/zlib.h>
#include <kern/thread.h>
#include <pexpert/pexpert.h>
#include <sys/errno.h>
#include <sys/fcntl.h>
#include <sys/mount.h>
#include <sys/uio.h>
#include <sys/vnode.h>
#include <sys/vnode_if.h>
#include "gpu_probe_payload.h"
#include "native_gpu_fake_core.h"

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

// The fake A740 (sheng-gpu-fake=1, QEMU only): Turnip's Darwin backend runs against it with no GPU behind it. See native_gpu_fake_core.h.
// Named after Apple's class because the original sandbox profiles admit that user client. Every submission counts as finished at once.
static constexpr uint32_t kFakeA740OpenType = 0x4147;

static void *
fakeAllocate(uint64_t bytes, uint64_t *actualBytes)
{
    const vm_size_t rounded = static_cast<vm_size_t>((bytes + PAGE_SIZE - 1) & ~static_cast<uint64_t>(PAGE_SIZE - 1));
    const IOOptionBits options = static_cast<IOOptionBits>(kIODirectionInOut) | static_cast<IOOptionBits>(kIOMemoryKernelUserShared);
    IOBufferMemoryDescriptor *memory = IOBufferMemoryDescriptor::inTaskWithOptions(kernel_task, options, rounded, PAGE_SIZE);
    if (!memory) {
        return nullptr;
    }
    bzero(memory->getBytesNoCopy(), rounded);
    *actualBytes = rounded;
    return memory;
}

static void
fakeRelease(void *memory)
{
    static_cast<IOBufferMemoryDescriptor *>(memory)->release();
}

static uint64_t
fakeNow()
{
    return mach_absolute_time();
}

class AGXDeviceUserClient : public IOUserClient {
    OSDeclareDefaultStructors(AGXDeviceUserClient)
public:
    bool start(IOService *provider) override;
    void free(void) override;
    IOReturn clientClose(void) override;
    IOReturn clientMemoryForType(UInt32 type, IOOptionBits *options, IOMemoryDescriptor **memory) override;
    IOReturn externalMethod(uint32_t selector, IOExternalMethodArguments *arguments, IOExternalMethodDispatch *dispatch, OSObject *target,
                            void *reference) override;
private:
    static constexpr uint32_t kCapacity = 8192;
    IOLock *lock_;
    FakeA740Entry *entries_;
    FakeA740Core core_;
};
OSDefineMetaClassAndStructors(AGXDeviceUserClient, IOUserClient)

bool
AGXDeviceUserClient::start(IOService *provider)
{
    if (!IOUserClient::start(provider)) {
        return false;
    }
    lock_ = IOLockAlloc();
    entries_ = static_cast<FakeA740Entry *>(IOMalloc(sizeof(FakeA740Entry) * kCapacity));
    if (!lock_ || !entries_) {
        return false;
    }
    bzero(entries_, sizeof(FakeA740Entry) * kCapacity);
    core_.init(entries_, kCapacity, FakeA740Hooks{fakeAllocate, fakeRelease, fakeNow});
    char name[20] = {};
    proc_selfname(name, sizeof(name));
    IOLog("NativeGpuFake: client opened by %s[%d]\n", name, proc_selfpid());
    return true;
}

void
AGXDeviceUserClient::free(void)
{
    if (entries_) {
        core_.releaseAll();
        IOFree(entries_, sizeof(FakeA740Entry) * kCapacity);
    }
    if (lock_) {
        IOLockFree(lock_);
    }
    IOUserClient::free();
}

IOReturn
AGXDeviceUserClient::clientClose(void)
{
    IOLog("NativeGpuFake: client closed after %llu submissions, %u buffers live\n", core_.submissions(), core_.live());
    if (!isInactive()) {
        terminate();
    }
    return kIOReturnSuccess;
}

IOReturn
AGXDeviceUserClient::clientMemoryForType(UInt32 type, IOOptionBits *options, IOMemoryDescriptor **memory)
{
    if (!options || !memory) {
        return kIOReturnBadArgument;
    }
    *memory = nullptr;
    IOLockLock(lock_);
    IOBufferMemoryDescriptor *buffer = static_cast<IOBufferMemoryDescriptor *>(core_.memoryForToken(type));
    if (buffer) {
        buffer->retain();
    }
    IOLockUnlock(lock_);
    if (!buffer) {
        return kIOReturnNotFound;
    }
    *memory = buffer;
    *options = kIOMapAnywhere;
    return kIOReturnSuccess;
}

IOReturn
AGXDeviceUserClient::externalMethod(uint32_t selector, IOExternalMethodArguments *arguments, IOExternalMethodDispatch *, OSObject *, void *)
{
    if (!arguments) {
        return kIOReturnBadArgument;
    }
    IOLockLock(lock_);
    const uint64_t before = core_.submissions();
    const uint32_t status = core_.call(selector, arguments->structureInput, arguments->structureInputSize, arguments->structureOutput, arguments->structureOutputSize);
    const bool report = core_.submissions() != before && core_.firstOfPowerOfTwoSubmission();
    const uint64_t submissions = core_.submissions();
    IOLockUnlock(lock_);
    if (report) {   // 1, 2, 4, 8...: the boot log keeps only its last 80 KB
        char name[20] = {};
        proc_selfname(name, sizeof(name));
        IOLog("NativeGpuFake: %s[%d] submission %llu\n", name, proc_selfpid(), submissions);
    }
    return static_cast<IOReturn>(status);
}

// Named after Apple's class, as the hardware front end is: Turnip finds the service by this name.
class AGXAccelerator : public IOAccelerator {
    OSDeclareDefaultStructors(AGXAccelerator)
public:
    using IOService::newUserClient;
    bool start(IOService *provider) override;
    IOReturn newUserClient(task_t owningTask, void *securityID, UInt32 type, OSDictionary *properties,
                           IOUserClient **handler) override;
};
OSDefineMetaClassAndStructors(AGXAccelerator, IOAccelerator)

#define PROBE_ROOT "/System/Volumes/Data/Library/GPUBundles"
#define PROBE_BUNDLE PROBE_ROOT "/" SHENG_GPU_PROBE_NAME ".bundle"
#define REAL_NODE "/arm-io/native-gpu-plugin"
#define REAL_NAME "NativeAdrenoMetal"
#define REAL_CLASS "NativeAdrenoMetalDevice"
#define REAL_BUNDLE PROBE_ROOT "/" REAL_NAME ".bundle"

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

static voidpf
probeInflateAlloc(voidpf, uInt items, uInt size)
{
    vm_size_t bytes = vm_size_t(items) * size + sizeof(vm_size_t);
    vm_size_t *block = static_cast<vm_size_t *>(IOMalloc(bytes));
    if (!block) {
        return Z_NULL;
    }
    *block = bytes;
    return block + 1;
}

static void
probeInflateFree(voidpf, voidpf address)
{
    vm_size_t *block = static_cast<vm_size_t *>(address) - 1;
    IOFree(block, *block);
}

// A property is the file's length (64-bit little endian) and then its zlib stream: DarwinBoot refuses a device tree over 16 MiB.
static int
probeWriteProperty(IORegistryEntry *node, const char *property, const char *path, int mode, vfs_context_t context)
{
    OSData *data = OSDynamicCast(OSData, node->getProperty(property));
    uint64_t fullBytes = 0;
    if (!data || data->getLength() <= sizeof(fullBytes)) {
        IOLog("NativeGpuProbe: %s is missing from the device tree\n", property);
        return EINVAL;
    }
    const unsigned char *packed = static_cast<const unsigned char *>(data->getBytesNoCopy());
    memcpy(&fullBytes, packed, sizeof(fullBytes));
    if (!fullBytes || fullBytes > (64ull << 20)) {
        IOLog("NativeGpuProbe: %s has an unreasonable length %llu\n", property, fullBytes);
        return EINVAL;
    }
    unsigned char *full = static_cast<unsigned char *>(IOMalloc(fullBytes));
    if (!full) {
        return ENOMEM;
    }
    z_stream stream = {};
    stream.next_in = const_cast<Bytef *>(packed + sizeof(fullBytes));
    stream.avail_in = data->getLength() - sizeof(fullBytes);
    stream.next_out = full;
    stream.avail_out = static_cast<uInt>(fullBytes);
    stream.zalloc = probeInflateAlloc;
    stream.zfree = probeInflateFree;
    int inflated = inflateInit(&stream);
    if (inflated == Z_OK) {
        inflated = inflate(&stream, Z_FINISH);
        inflateEnd(&stream);
    }
    int error = inflated == Z_STREAM_END && stream.total_out == fullBytes ? probeWriteFile(path, full, fullBytes, mode, context) : EINVAL;
    IOFree(full, fullBytes);
    IOLog("NativeGpuProbe: wrote %s: %d (%llu bytes, inflate %d)\n", path, error, fullBytes, inflated);
    return error;
}

static bool
probeInstallRealPlugin(vfs_context_t context, unsigned waited)
{
    IORegistryEntry *node = IORegistryEntry::fromPath(REAL_NODE, gIODTPlane);
    if (!node) {
        return false;
    }
    static const char *const directories[] = {PROBE_ROOT, REAL_BUNDLE, REAL_BUNDLE "/Contents", REAL_BUNDLE "/Contents/MacOS"};
    for (const char *directory : directories) {
        int error = probeMakeDirectory(directory);
        if (error) {
            IOLog("NativeGpuProbe: mkdir %s failed: %d\n", directory, error);
            node->release();
            return true;
        }
    }
    int failed = probeWriteProperty(node, "info-plist", REAL_BUNDLE "/Contents/Info.plist", 0644, context);
    failed |= probeWriteProperty(node, "executable", REAL_BUNDLE "/Contents/MacOS/" REAL_NAME, 0755, context);
    failed |= probeWriteProperty(node, "turnip", REAL_BUNDLE "/Contents/MacOS/libvulkan_freedreno.dylib", 0755, context);
    IOLog("NativeGpuProbe: real plugin install after %u s: %s\n", waited, failed ? "FAILED" : "complete");
    node->release();
    return true;
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
    if (probeInstallRealPlugin(context, waited)) {
        return;
    }
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
AGXAccelerator::start(IOService *provider)
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
    IORegistryEntry *real = IORegistryEntry::fromPath(REAL_NODE, gIODTPlane);
    if (real) {
        setProperty("MetalPluginName", REAL_NAME);
        setProperty("MetalPluginClassName", REAL_CLASS);
        real->release();
    }
    thread_t thread = THREAD_NULL;
    if (kernel_thread_start(probeInstallThread, nullptr, &thread) == KERN_SUCCESS) {
        thread_deallocate(thread);
    } else {
        IOLog("NativeGpuProbe: could not start the bundle installer\n");
    }
    registerService();
    IOLog("NativeGpuProbe: IOAccelerator service published for plugin %s\n", real ? REAL_NAME : SHENG_GPU_PROBE_NAME);
    return true;
}

IOReturn
AGXAccelerator::newUserClient(task_t owningTask, void *securityID, UInt32 type, OSDictionary *properties,
                                         IOUserClient **handler)
{
    char name[20] = {};
    proc_selfname(name, sizeof(name));
    IOLog("NativeGpuProbe: newUserClient from %s[%d] type 0x%x\n", name, proc_selfpid(), (unsigned)type);
    uint32_t fake = 0;
    const bool useFake = PE_parse_boot_argn("sheng-gpu-fake", &fake, sizeof(fake)) && fake == 1 && type == kFakeA740OpenType;
    IOUserClient *client = useFake ? static_cast<IOUserClient *>(OSTypeAlloc(AGXDeviceUserClient)) : OSTypeAlloc(NativeGpuProbeUserClient);
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
