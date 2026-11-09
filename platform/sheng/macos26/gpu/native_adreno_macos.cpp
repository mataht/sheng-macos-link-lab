// SPDX-License-Identifier: GPL-3.0-or-later
// macOS front end of the Adreno 740 kernel stack from UniXNU (Adreno740, the SM8550 kits and the board's GpuPowerProbe.cpp).
//
// Default off: nothing runs without sheng-gpu-power=<1..9>, and the probe refuses to start in a VM (QEMU has no A740).
//   1..3 read clocks and power, 4 GMU SMMU context, 5..7 GMU bring-up, 8 GX power, 9 GPU start and command processor.
// Stage 9 publishes the GPU service, which matches AGXAccelerator below by personality. sheng-gpu-metal=1 also installs the
// Metal plugin a local package carries in /arm-io/native-gpu-plugin (see pluginInstallThread).
//
// Vendor firmware is never compiled in. A local package adds /arm-io/native-gpu with the properties sqe-firmware, gmu-firmware
// and zap-firmware, each accepted only when its size and SHA-256 equal the pins. Missing or rejected firmware stops the bring-up
// at the first stage that needs it.
//
// This file also supplies what the iPadOS board build gets from other board files: the status rows the probe writes (printed
// here on the kernel console, since the iPadOS status page does not exist) and inert definitions of diagnostics we do not build.
#include <IOKit/IOService.h>
#include <IOKit/IOUserClient.h>
#include <IOKit/IOLib.h>
#include <IOKit/IOMemoryDescriptor.h>
#include <IOKit/IODeviceTreeSupport.h>
#include <kern/thread.h>
#include <libkern/c++/OSData.h>
#include <libkern/crypto/sha2.h>
#include <libkern/zlib.h>
#include <pexpert/pexpert.h>
#include <string.h>
#include <sys/errno.h>
#include <sys/fcntl.h>
#include <sys/mount.h>
#include <sys/uio.h>
#include <sys/vnode.h>
#include <sys/vnode_if.h>
#include "adreno_firmware_pins.h"

extern "C" {
void sheng_gpu_power_probe(void);
int sheng_kernel_mkdir(const char *path, int mode);   // patches/0067
IOReturn native_adreno_new_user_client(IOService *, task_t, void *, UInt32, OSDictionary *, IOUserClient **, unsigned);

// Firmware the board probe uses; filled by loadFirmware() before the probe starts.
const uint8_t *native_adreno_sqe_firmware_image;
uint64_t native_adreno_sqe_firmware_image_size;
const uint8_t *native_adreno_zap_image;
uint64_t native_adreno_zap_image_size;
const uint8_t *native_adreno_firmware_image;
uint64_t native_adreno_firmware_image_size;

// Status rows of the iPadOS boot page; here the reporter thread prints them.
char boot_progress_ext[8][48];
char boot_progress_ext_wide[2][112];

// iPadOS-only diagnostics and integrations: present so the shared code links, inert here.
volatile int native_gpu_addon_stage, native_gpu_addon_status, native_gpu_addon2_stage, native_gpu_addon2_status;
volatile uint32_t native_hid_input_events[8];
volatile uint64_t native_adreno_fence_phys;   // written by the probe; defined upstream in a service file we do not build
int boot_latency_enabled(void) {return 0;}
void boot_latency_record(uint32_t, uint64_t, uint64_t, uint64_t, uint64_t, uint64_t, uint64_t) {}
int boot_log_export_put(uint32_t, uint32_t, uint32_t, const void *, uint32_t) {return -1;}
unsigned int native_pmap_user_mappings(ppnum_t, void **, uint64_t *, char *) {return 0;}
// The Metal scanout hand-off needs the macOS display driver's side, which does not exist yet.
IOReturn native_gop_handoff_call(uint32_t, task_t, const void *, size_t, void *, size_t) {return kIOReturnUnsupported;}
IOMemoryDescriptor *native_gop_handoff_memory(void) {return nullptr;}
}

// IOGraphicsFamily's class: no virtual method beyond IOService's. The storage-module audit compares every slot of the subclass
// below with the original IOAccelerator table (272 slots).
class IOAccelerator : public IOService {
    OSDeclareDefaultStructors(IOAccelerator)
};

// Named after Apple's class, as upstream does. The original sandbox profiles allow services derived from IOAccelerator.
class AGXAccelerator : public IOAccelerator {
    OSDeclareDefaultStructors(AGXAccelerator)
public:
    using IOService::newUserClient;
    bool start(IOService *provider) override;
    IOReturn newUserClient(task_t owningTask, void *securityID, UInt32 type, OSDictionary *properties,
                           IOUserClient **handler) override;
};
OSDefineMetaClassAndStructors(AGXAccelerator, IOAccelerator)

bool
AGXAccelerator::start(IOService *provider)
{
    if (!IOAccelerator::start(provider)) {
        return false;
    }
    registerService();
    IOLog("NativeGpu: IOAccelerator published on %s\n", provider->getName());
    return true;
}

IOReturn
AGXAccelerator::newUserClient(task_t owningTask, void *securityID, UInt32 type, OSDictionary *properties, IOUserClient **handler)
{
    return native_adreno_new_user_client(this, owningTask, securityID, type, properties, handler, 0);
}

namespace {

const uint8_t *
acceptedCopy(IORegistryEntry *node, const NativeGpuFirmwarePin &pin, uint64_t *size)
{
    OSData *data = OSDynamicCast(OSData, node->getProperty(pin.property));
    if (!data) {
        IOLog("NativeGpu: %s is absent\n", pin.property);
        return nullptr;
    }
    uint8_t digest[32];
    SHA256_CTX context;
    SHA256_Init(&context);
    SHA256_Update(&context, data->getBytesNoCopy(), data->getLength());
    SHA256_Final(digest, &context);
    if (data->getLength() != pin.bytes || memcmp(digest, pin.sha256, sizeof(digest))) {
        IOLog("NativeGpu: %s rejected (%u bytes): size or SHA-256 differs from the pin\n", pin.property, data->getLength());
        return nullptr;
    }
    uint8_t *copy = static_cast<uint8_t *>(IOMalloc(data->getLength()));
    if (!copy) {
        return nullptr;
    }
    memcpy(copy, data->getBytesNoCopy(), data->getLength());
    *size = data->getLength();
    return copy;
}

void
loadFirmware()
{
    IORegistryEntry *node = IORegistryEntry::fromPath("/arm-io/native-gpu", gIODTPlane);
    if (!node) {
        IOLog("NativeGpu: no /arm-io/native-gpu node; the bring-up stops at the first stage that needs firmware\n");
        return;
    }
    native_adreno_sqe_firmware_image = acceptedCopy(node, kNativeGpuFirmwarePins[0], &native_adreno_sqe_firmware_image_size);
    native_adreno_firmware_image = acceptedCopy(node, kNativeGpuFirmwarePins[1], &native_adreno_firmware_image_size);
    native_adreno_zap_image = acceptedCopy(node, kNativeGpuFirmwarePins[2], &native_adreno_zap_image_size);
    node->release();
    IOLog("NativeGpu: firmware sqe %llu gmu %llu zap %llu bytes\n", native_adreno_sqe_firmware_image_size,
          native_adreno_firmware_image_size, native_adreno_zap_image_size);
}

// The real Metal plugin, for a local package only. /arm-io/native-gpu-plugin holds the bundle's files, each as its length (64-bit
// little endian) and a zlib stream, because DarwinBoot refuses a device tree over 16 MiB. With sheng-gpu-metal=1 and stage 9 the files
// are written to the Data volume once it is mounted. On the tablet that volume sits behind the volatile RAM overlay, so nothing
// reaches the partition. The two CDHashes must be in the package's trust cache and lp-sip0 needs bit 1 (both done by the packager).
#define PLUGIN_NODE "/arm-io/native-gpu-plugin"
#define PLUGIN_ROOT "/System/Volumes/Data/Library/GPUBundles"
#define PLUGIN_NAME "NativeAdrenoMetal"
#define PLUGIN_BUNDLE PLUGIN_ROOT "/" PLUGIN_NAME ".bundle"

int
pluginWriteFile(const char *path, const unsigned char *data, size_t size, int mode, vfs_context_t context)
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

voidpf
pluginInflateAlloc(voidpf, uInt items, uInt size)
{
    vm_size_t bytes = vm_size_t(items) * size + sizeof(vm_size_t);
    vm_size_t *block = static_cast<vm_size_t *>(IOMalloc(bytes));
    if (!block) {
        return Z_NULL;
    }
    *block = bytes;
    return block + 1;
}

void
pluginInflateFree(voidpf, voidpf address)
{
    vm_size_t *block = static_cast<vm_size_t *>(address) - 1;
    IOFree(block, *block);
}

int
pluginWriteProperty(IORegistryEntry *node, const char *property, const char *path, int mode, vfs_context_t context)
{
    OSData *data = OSDynamicCast(OSData, node->getProperty(property));
    uint64_t fullBytes = 0;
    if (!data || data->getLength() <= sizeof(fullBytes)) {
        IOLog("NativeGpu: plugin property %s is missing\n", property);
        return EINVAL;
    }
    const unsigned char *packed = static_cast<const unsigned char *>(data->getBytesNoCopy());
    memcpy(&fullBytes, packed, sizeof(fullBytes));
    if (!fullBytes || fullBytes > (64ull << 20)) {
        IOLog("NativeGpu: plugin property %s has an unreasonable length %llu\n", property, fullBytes);
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
    stream.zalloc = pluginInflateAlloc;
    stream.zfree = pluginInflateFree;
    int inflated = inflateInit(&stream);
    if (inflated == Z_OK) {
        inflated = inflate(&stream, Z_FINISH);
        inflateEnd(&stream);
    }
    int error = inflated == Z_STREAM_END && stream.total_out == fullBytes ? pluginWriteFile(path, full, fullBytes, mode, context) : EINVAL;
    IOFree(full, fullBytes);
    IOLog("NativeGpu: plugin wrote %s: %d (%llu bytes, inflate %d)\n", path, error, fullBytes, inflated);
    return error;
}

void
pluginInstallThread(void *, wait_result_t)
{
    vfs_context_t context = vfs_context_kernel();
    vnode_t vp = NULLVP;
    // /System/Volumes/Data is an empty mount point until the boot tasks mount the Data volume.
    unsigned polls = 0;
    while (vnode_lookup("/System/Volumes/Data/Library", 0, &vp, context) != 0) {
        if (++polls > 2400) {
            IOLog("NativeGpu: the Data volume did not appear in 10 minutes; plugin not installed\n");
            return;
        }
        IOSleep(250);
    }
    vnode_put(vp);
    IORegistryEntry *node = IORegistryEntry::fromPath(PLUGIN_NODE, gIODTPlane);
    if (!node) {
        return;
    }
    static const char *const directories[] = {PLUGIN_ROOT, PLUGIN_BUNDLE, PLUGIN_BUNDLE "/Contents", PLUGIN_BUNDLE "/Contents/MacOS"};
    for (const char *directory : directories) {
        int error = sheng_kernel_mkdir(directory, 0755);
        if (error && error != EEXIST) {
            IOLog("NativeGpu: plugin mkdir %s failed: %d\n", directory, error);
            node->release();
            return;
        }
    }
    int failed = pluginWriteProperty(node, "info-plist", PLUGIN_BUNDLE "/Contents/Info.plist", 0644, context);
    failed |= pluginWriteProperty(node, "executable", PLUGIN_BUNDLE "/Contents/MacOS/" PLUGIN_NAME, 0755, context);
    failed |= pluginWriteProperty(node, "turnip", PLUGIN_BUNDLE "/Contents/MacOS/libvulkan_freedreno.dylib", 0755, context);
    IOLog("NativeGpu: plugin install after %u polls: %s\n", polls, failed ? "FAILED" : "complete");
    node->release();
}

void
startPluginInstall(unsigned mode)
{
    unsigned metal = 0;
    if (mode < 9 || !PE_parse_boot_argn("sheng-gpu-metal", &metal, sizeof(metal)) || metal != 1) {
        return;
    }
    IORegistryEntry *node = IORegistryEntry::fromPath(PLUGIN_NODE, gIODTPlane);
    if (!node) {
        IOLog("NativeGpu: sheng-gpu-metal=1 but no %s node; plugin not installed\n", PLUGIN_NODE);
        return;
    }
    node->release();
    thread_t thread = THREAD_NULL;
    if (kernel_thread_start(pluginInstallThread, nullptr, &thread) == KERN_SUCCESS) {
        thread_deallocate(thread);
    }
}

// The probe keeps its state in the status rows; print each row when it changes, for the first ten minutes. Every 20 s of the first
// two minutes all rows are printed again, because the boot log scrolls past and a photo may catch only part of it.
void
reporterThread(void *, wait_result_t)
{
    static char lastRows[8][48], lastWide[2][112];
    for (unsigned seconds = 0; seconds < 600; seconds += 2) {
        IOSleep(2000);
        const bool again = seconds < 120 && seconds % 20 == 18;
        for (unsigned row = 0; row < 8; ++row) {
            if (boot_progress_ext[row][0] && (again || memcmp(lastRows[row], boot_progress_ext[row], sizeof(lastRows[row])))) {
                memcpy(lastRows[row], boot_progress_ext[row], sizeof(lastRows[row]));
                IOLog("NativeGpu row%u t%us: %.47s\n", row, seconds, lastRows[row]);
            }
        }
        for (unsigned row = 0; row < 2; ++row) {
            if (boot_progress_ext_wide[row][0] && (again || memcmp(lastWide[row], boot_progress_ext_wide[row], sizeof(lastWide[row])))) {
                memcpy(lastWide[row], boot_progress_ext_wide[row], sizeof(lastWide[row]));
                IOLog("NativeGpu wide%u t%us: %.111s\n", row, seconds, lastWide[row]);
            }
        }
    }
}

}   // namespace

class NativeGpuStart : public IOService {
    OSDeclareDefaultStructors(NativeGpuStart)
public:
    bool start(IOService *provider) override;
};
OSDefineMetaClassAndStructors(NativeGpuStart, IOService)

bool
NativeGpuStart::start(IOService *provider)
{
    unsigned mode = 0;
    if (!PE_parse_boot_argn("sheng-gpu-power", &mode, sizeof(mode)) || !mode) {
        return false;
    }
    if (PE_vmm_present != 0) {
        IOLog("NativeGpu: refused in a VM: QEMU has no Adreno 740\n");
        return false;
    }
    if (!IOService::start(provider)) {
        return false;
    }
    IOLog("NativeGpu: starting bring-up mode %u\n", mode);
    loadFirmware();
    startPluginInstall(mode);
    sheng_gpu_power_probe();
    thread_t thread = THREAD_NULL;
    if (kernel_thread_start(reporterThread, nullptr, &thread) == KERN_SUCCESS) {
        thread_deallocate(thread);
    }
    return true;
}
