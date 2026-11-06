// SPDX-License-Identifier: BSD-2-Clause-Patent
// Adapted from Droid-VM/macOS-DroidVM@031c35a for native sheng GOP scanout.
// Native macOS 26.5 GOP scanout using the original IOMobileGraphicsFamily swap queue.
// Scanout is CPU-copied; no accelerated GPU service or VM identity is published.
// Private object offsets follow the paired 25F71 legacy framebuffer ABI.
#include "IOMobileFramebufferABI.h"
#include <pexpert/pexpert.h>
#include <machine/machine_routines.h>
#include <mach/mach_time.h>
// Exact paired pexpert.h XNU_KERNEL_PRIVATE declaration; VM opt-in only.
extern "C" uint32_t PE_vmm_present;
extern "C" volatile uint64_t sheng_panel_ready, sheng_status_pixels;
#include <IOKit/IOLib.h>
#include "gop_scanout.h"
#include "framebuffer_owner.h"
#include "gop_layers.h"
#include <IOKit/IOPlatformExpert.h>
#include <IOKit/IOTimerEventSource.h>
#include <IOKit/IOUserClient.h>
#include <IOKit/IOWorkLoop.h>
#include <IOKit/pwr_mgt/RootDomain.h>
#include <libkern/c++/OSArray.h>
#include <libkern/c++/OSDictionary.h>
#include <libkern/c++/OSNumber.h>
#include <libkern/c++/OSString.h>

extern "C" task_t kernel_task;

// IOMobileFramebufferLegacy (instance 0x1af8)
static constexpr size_t kCurrentSwap = 6032;       // the swap being programmed (swap_begin_gated 0x...dba5c, clean_queue_gated)
static constexpr size_t kSwapQueueFirst = 6040;    // TAILQ head of the pending swaps
static constexpr size_t kSwapQueueLast = 6048;     // its tqh_last
static constexpr size_t kDisplayWidth = 6400;      // display_width() / display_height() (set_digital_out_mode block 0x...d6a60)
static constexpr size_t kDisplayHeight = 6404;
static constexpr size_t kSystemPowerState = 6428;  // u32: setPowerState's state (do_power_state_change_action_gated, request type 0)
static constexpr size_t kAPIPowerState = 6432;     // u32: setAPIPowerState's state (power_state_change_interrupt, request type 1)
static constexpr size_t kHardwarePowered = 6436;   // u32: the display is powered ("PowerStates-Cntr" in swap_wait_gated's report); with
                                                   // kHardwareStarted set, swap_wait fails kIOReturnOffline while it is 0 (0x...2d1e4)
static constexpr size_t kSwapInfoHeld = 6902;      // u16: swap info notifications are held (notify_swap_done_gated 0x...d9430)
static constexpr size_t kSwapInfoTime = 264;       // the swap info's time (notify_swap_done_gated 0x...d93f0)
// IOMobileFramebuffer (instance 144): set before power management in AppleParavirtDisplay's start_hardware block (0x...d8e90)
static constexpr size_t kHardwareStarted = 140;
// IOMFBSwapIORequest
static constexpr size_t kSwapLayers = 824;         // bit 0: main surface, bit 1: cursor surface (program_hardware_gated 0x...db858)
static constexpr size_t kSwapDropReasons = 844;    // programmed when nothing but bit 3 is set, else dropped (swap_begin_gated 0x...dbac0)
static constexpr size_t kSwapInfoA = 752;         // swap info words 3 and 4 (notify_swap_done_gated 0x...d93f4)
static constexpr size_t kSwapInfoB = 816;
static constexpr size_t kSwapMainSurface = 2752;   // Four surface pointers follow at pointer-size strides.
static constexpr size_t kSwapNext = 3192;          // TAILQ entry of the queue
static constexpr size_t kSwapPrev = 3200;
// IOMFBNotifyRequest
static constexpr size_t kNotifyAsyncReference = 24;   // OSAsyncReference64 of the user client (do_swap_info_notify_gated 0x...d9370)
static constexpr size_t kNotifyMask = 120;            // which swap info words the client wants; bit 31: as 32-bit halves
static constexpr size_t kNotifyLastTime = 128;        // the time of the previous notification (word 1 is the delta)
// IOAVElement: the element ID and its score (createTimingElementWithDimensionsAndRate 0x...d8974, createColorElementWithDepthAndEDR)
static constexpr size_t kElementID = 16;
static constexpr size_t kElementScore = 24;

template <typename T> static T &at(void *base, size_t offset) {return *reinterpret_cast<T *>(static_cast<uint8_t *>(base) + offset);}

// Kernel interfaces of the collection's kexts with no header here.
void iomfbUpdateSwapInfoState(IOMobileFramebufferLegacy *, IOMFBSwapIORequest *, uint32_t)
    __asm("__ZN25IOMobileFramebufferLegacy28update_swap_info_state_gatedEP18IOMFBSwapIORequest24IOMFBSwapCompletionState");
void iomfbSwapInfoDispatch(IOMobileFramebufferLegacy *, void *)
    __asm("__ZN25IOMobileFramebufferLegacy31swap_info_notify_dispatch_gatedEPNS_12SwapInfoBlobE");
IOReturn userClientSendAsyncResult(io_user_reference_t *reference, IOReturn, io_user_reference_t *args, UInt32 count, IOOptionBits)
    __asm("__ZN12IOUserClient28sendAsyncResult64WithOptionsEPyiS0_jj");   // protected in IOUserClient.h
uint64_t ioSurfaceWidth(const IOSurface *) __asm("__ZNK9IOSurface8getWidthEv");
uint64_t ioSurfaceHeight(const IOSurface *) __asm("__ZNK9IOSurface9getHeightEv");
uint64_t ioSurfaceBytesPerRow(const IOSurface *) __asm("__ZNK9IOSurface14getBytesPerRowEv");
uint64_t ioSurfaceBaseOffset(const IOSurface *) __asm("__ZNK9IOSurface13getBaseOffsetEv");
uint32_t ioSurfacePixelFormat(const IOSurface *) __asm("__ZNK9IOSurface14getPixelFormatEv");
IOMemoryDescriptor *ioSurfaceMemory(const IOSurface *) __asm("__ZNK9IOSurface19getMemoryDescriptorEv");
// IOAVVideoTimingData: per axis (horizontal, vertical) 32 bytes of which total +4, active +8, sync width +12, back porch +16,
// front porch +20, sync rate +24, sync polarity +28, then the pixel clock at +68 (IOAVVideoTimingElement's getters; the element keeps
// the data at +32).
OSObject *ioavTimingWithData(const void *data, uint32_t standardType, uint32_t timingStandard)
    __asm("__ZN22IOAVVideoTimingElement8withDataEPK19IOAVVideoTimingData16IOAVStandardType23IOAVVideoTimingStandard");
void ioavTimingSetColorElements(OSObject *, OSArray *) __asm("__ZN22IOAVVideoTimingElement16setColorElementsEP7OSArray");
OSObject *ioavColorWithCapabilities(uint32_t standardType, uint32_t depth, uint32_t encoding, uint32_t range, uint32_t eotf,
                                    uint32_t colorimetry, bool, const OSDictionary *, const OSDictionary *, const OSDictionary *,
                                    const OSSet *)
    __asm("__ZN21IOAVVideoColorElement16withCapabilitiesE16IOAVStandardTypej22IOAVVideoPixelEncoding26IOAVVideoColorDynamicRange"
          "18IOAVVideoColorEOTF20IOAVVideoColorimetrybPK12OSDictionaryS7_S7_PK5OSSet");

static_assert(sizeof(IOMobileFramebuffer) == 144, "paired framebuffer base size");
static_assert(sizeof(IOMobileFramebufferLegacy) == 6904, "paired legacy framebuffer size");

class NativeGOPDisplay : public IOMobileFramebufferLegacy {
    OSDeclareDefaultStructors(NativeGOPDisplay)
public:
    bool start(IOService *) override;
    void stop(IOService *) override;
    bool start_hardware() override;
    IOReturn stop_hardware_gated() override;
    IOReturn swap_begin_gated() override;
    IOReturn swap_wait(IOMobileFramebufferUserClient *, unsigned int, unsigned int, unsigned int) override;
    IOReturn verify_swap(IOMFBSwapIORequest *) override;
    IOReturn swap_layer_map(IOMFBSwapIORequest *, unsigned int) override;
    IOReturn surface_layer_map(IOMFB::AP::DisplayedData *, unsigned int, IOSurface *) override;
    IOReturn surface_layer_unmap(IOMFB::AP::DisplayedData *, unsigned int) override;
    IOReturn set_display_device(unsigned int) override;
    IOReturn set_digital_out_mode(unsigned int, unsigned int) override;
    IOReturn get_dot_pitch(unsigned int *) override;
    IOReturn get_gamma_table(IOMFBGammaTable *) override;
    IOReturn set_gamma_table(IOMFBGammaTable *) override;
    IOReturn set_color_remap_mode(DisplayColorRemapMode) override;
    IOReturn get_color_remap_mode(DisplayColorRemapMode *) const override;
    IOReturn swap_set_color_matrix(IOMFBColorFixedMatrix *, IOMFBColorMatrixFunction, unsigned int) override;
    IOReturn set_notify_tag_mask_gated(IOMFB_NotificationType, IOMFBNotifyRequest *, unsigned int) override;
    IOReturn supported_frame_info(unsigned int, unsigned int *, char (*)[64], unsigned int *) override;
    IOReturn factory_portal(void *, task *) override;
    IOReturn executeFactoryConfigurations_gated() override;
    IOReturn set_frame_info_notification_state_gated(IOMFBNotifyRequest *, bool) override;
    IOReturn do_swap_info_notify_gated(IOMFBNotifyRequest *, const SwapInfoBlob *) override;
    IOReturn notify_swap_done_gated(unsigned int, ::SwapInfoBlob *, unsigned int *, unsigned int) override;
    IOReturn notify_crc_ready_gated(unsigned int) override;
    bool are_timings_enabled() const override;
    IOReturn do_power_state_change_gated() override;

private:
    static constexpr uint32_t kRefreshHz = 60;
    static constexpr uint32_t kColorID = 100, kTimingID = 1000;  // AppleParavirtDisplay's IDs of its first color and timing element

    bool clean_queue_gated();
    void program_hardware_gated();
    void copySurface(IOSurface *);
    const uint8_t *mapSource(IOMemoryDescriptor *, uint64_t base, uint64_t span);
    void releaseSourceMaps();
    bool createDisplayAttributes();
    static void vblFired(OSObject *, IOTimerEventSource *);

    struct SwapInfo {   // IOMobileFramebufferLegacy::SwapInfoBlob as AppleParavirtDisplay fills it (48 bytes)
        uint32_t swapID;
        uint64_t words[5];
    };
    static_assert(sizeof(SwapInfo) == 48, "IOMobileFramebufferLegacy::SwapInfoBlob");
    static constexpr unsigned kSwapInfoRing = 6;

    PE_Video fVideo{};
    IOMemoryMap *fScanoutMap = nullptr;
    uint8_t *fHalfToByte = nullptr;     // 'RGhA' surfaces: each half-float channel's 8-bit value, clamped to [0, 1]
    uint16_t *fRow = nullptr;           // one 'RGhA' row
    uint64_t fRowBytes = 0;
    uint8_t *fScanout = nullptr;
    IOTimerEventSource *fVBL = nullptr;
    DisplayColorRemapMode fColorRemapMode = static_cast<DisplayColorRemapMode>(0);
    uint32_t fSurfaceFormat = 0;
    uint64_t fSourceWidth = 0, fSourceHeight = 0;
    bool fDisplayOn = false;
    uint64_t fFrames = 0;
    uint64_t fLastLit = 0, fLastCenter = 0;   // bring-up: lit pixels and centre pixel of the last frame shown
    uint64_t fTicks = 0, fDropped = 0, fReported = 0;   // swap_begin_gated runs, swaps dropped, fSubmitted + fFrames + fDropped + fStalled at the last report
    uint64_t fWaitFailures = 0;                 // diagnostic: swap_wait calls that failed (WindowServer's display readiness)
    uint64_t fSubmitted = 0, fStalled = 0;          // swaps verified, VBL ticks with a queued swap clean_queue_gated did not hand over
    SwapInfo fSwapInfo[kSwapInfoRing]{};   // held while kSwapInfoHeld is set, sent in order afterwards
    unsigned fSwapInfoRead = 0, fSwapInfoWrite = 0;
    uint64_t fLayerShape = 0;
    // Copy-path state, appended after every earlier field so their offsets are unchanged.
    struct SourceMap {
        IOMemoryDescriptor *memory;   // retained and prepared while cached
        IOMemoryMap *map;             // read-only kernel mapping of [base, base + span)
        uint64_t base, span, used;
    };
    static constexpr unsigned kSourceMapCount = 3;      // WindowServer rotates a few root surfaces
    static constexpr uint64_t kFullRefreshFrames = 300; // rewrite every row periodically regardless of the shadow
    SourceMap fSourceMaps[kSourceMapCount]{};
    uint64_t fSourceMapClock = 0;
    uint32_t *fShadow = nullptr;        // what this driver last wrote to each scanout row
    uint64_t fShadowBytes = 0;
    uint32_t *fConverted = nullptr;     // one converted scanout row
    uint64_t fConvertedBytes = 0;
    bool fShadowValid = false;
    uint64_t fRowsWritten = 0, fRowsSkipped = 0;   // diagnostic: scanout rows rewritten / left unchanged
};

OSDefineMetaClassAndStructors(NativeGOPDisplay, IOMobileFramebufferLegacy)

// Explicit VM diagnostic policy. These are requests to the original PM
// user-activity consumer, not HID events, a display assertion, or power overrides.
// State is file-local so the paired NativeGOPDisplay class/virtual ABI is unchanged.
static IOService *gVMKeepAwakeOwner = nullptr;
static uint64_t gVMKeepAwakeDeadline = UINT64_MAX;

static bool nativeGopStartVMKeepAwake(IOService *owner)
{
    IOPMrootDomain *root = IOService::getPMRootDomain();
    if (!root) {
        IOLog("NativeGOPDisplay: VM keep-awake root domain not ready\n");
        return false;
    }
    IOService *expected = nullptr;
    if (!__atomic_compare_exchange_n(&gVMKeepAwakeOwner, &expected, owner, false,
                                      __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) {
        IOLog("NativeGOPDisplay: VM keep-awake already has an owner\n");
        return false;
    }
    uint64_t deadline;
    clock_interval_to_deadline(30, kSecondScale, &deadline);
    __atomic_store_n(&gVMKeepAwakeDeadline, deadline, __ATOMIC_RELEASE);
    root->requestUserActive(owner, "sheng-qemu-display-observation");
    IOLog("NativeGOPDisplay: VM requestUserActive submitted (start)\n");
    return true;
}

static void nativeGopVMKeepAwakeTick(IOService *owner)
{
    if (__atomic_load_n(&gVMKeepAwakeOwner, __ATOMIC_ACQUIRE) != owner)
        return;
    uint64_t deadline = __atomic_load_n(&gVMKeepAwakeDeadline, __ATOMIC_ACQUIRE);
    if (deadline == UINT64_MAX || mach_absolute_time() < deadline)
        return;
    uint64_t next;
    clock_interval_to_deadline(30, kSecondScale, &next);
    if (!__atomic_compare_exchange_n(&gVMKeepAwakeDeadline, &deadline, next, false,
                                      __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE))
        return;
    IOPMrootDomain *root = IOService::getPMRootDomain();
    if (!root) {
        IOLog("NativeGOPDisplay: VM keep-awake root domain unavailable; no request\n");
        return;
    }
    root->requestUserActive(owner, "sheng-qemu-display-observation");
    IOLog("NativeGOPDisplay: VM requestUserActive submitted (periodic)\n");
}

static IOPMPowerState gPowerStates[2] = {   // AppleParavirtDisplay's gPowerState
    {1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    {1, kIOPMDeviceUsable, kIOPMPowerOn, kIOPMPowerOn, 0, 0, 0, 0, 0, 0, 0, 0},
};

bool NativeGOPDisplay::start(IOService *provider)
{
    unsigned enabled = 0, cpus = 0, vmKeepAwake = 0;
    PE_parse_boot_argn("native-gop-vm-keep-awake", &vmKeepAwake, sizeof(vmKeepAwake));
    if (vmKeepAwake > 1 || (vmKeepAwake == 1 && PE_vmm_present != 1)) {
        IOLog("NativeGOPDisplay: VM keep-awake requires explicit VMM opt-in\n");
        return false;
    }
    PE_parse_boot_argn("native-gop-display", &enabled, sizeof(enabled));
    PE_parse_boot_argn("cpus", &cpus, sizeof(cpus));
    if (enabled != 1 || (cpus != 1 && cpus != 2 && cpus != 4 && cpus != 8) || !provider)
        return false;
    if (getPlatform()->getConsoleInfo(&fVideo) != kIOReturnSuccess || !fVideo.v_baseAddr || fVideo.v_depth != 32 ||
        !fVideo.v_width || !fVideo.v_height || fVideo.v_width > 16384 || fVideo.v_height > 16384 ||
        fVideo.v_rowBytes < fVideo.v_width * 4 ||
        fVideo.v_rowBytes > (512ULL << 20) / fVideo.v_height ||
        fVideo.v_baseAddr > UINT64_MAX - fVideo.v_rowBytes * fVideo.v_height) {
        IOLog("NativeGOPDisplay: no 32-bit boot framebuffer (base %#lx depth %lu)\n", fVideo.v_baseAddr, fVideo.v_depth);
        return false;
    }
    IOMemoryDescriptor *memory = IOMemoryDescriptor::withPhysicalAddress(fVideo.v_baseAddr & ~3UL, fVideo.v_rowBytes * fVideo.v_height,
                                                                         kIODirectionInOut);
    if (memory) {
        fScanoutMap = memory->createMappingInTask(kernel_task, 0, kIOMapAnywhere | kIOMapWriteCombineCache);
        memory->release();
    }
    if (!fScanoutMap) {
        IOLog("NativeGOPDisplay: cannot map the framebuffer at %#lx\n", fVideo.v_baseAddr);
        return false;
    }
    fScanout = reinterpret_cast<uint8_t *>(fScanoutMap->getVirtualAddress());
    IOLog("NativeGOPDisplay: scanout %#lx %lux%lu row %lu\n", fVideo.v_baseAddr, fVideo.v_width, fVideo.v_height, fVideo.v_rowBytes);
    if (!IOMobileFramebufferLegacy::start(provider)) {
        IOLog("NativeGOPDisplay: IOMobileFramebufferLegacy::start failed\n");
        OSSafeReleaseNULL(fScanoutMap);
        return false;
    }
    if (vmKeepAwake == 1 && !nativeGopStartVMKeepAwake(this)) {
        stop(provider);
        return false;
    }
    IOLog("NativeGOPDisplay: started, display %ux%u\n", display_width(), display_height());
    return true;
}

void NativeGOPDisplay::stop(IOService *provider)
{
    // Suspend before the base stops/removes the existing timer; release the
    // owner only after base stop has completed, so a new owner cannot race it.
    if (__atomic_load_n(&gVMKeepAwakeOwner, __ATOMIC_ACQUIRE) == this)
        __atomic_store_n(&gVMKeepAwakeDeadline, UINT64_MAX, __ATOMIC_RELEASE);
    IOMobileFramebufferLegacy::stop(provider);
    IOService *expected = this;
    __atomic_compare_exchange_n(&gVMKeepAwakeOwner, &expected, nullptr, false,
                                  __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE);
    if (fHalfToByte)
        IOFree(fHalfToByte, 65536);
    if (fRow)
        IOFree(fRow, fRowBytes);
    fHalfToByte = nullptr;
    fRow = nullptr;
    releaseSourceMaps();
    if (fShadow)
        IOFreeData(fShadow, fShadowBytes);
    if (fConverted)
        IOFreeData(fConverted, fConvertedBytes);
    fShadow = fConverted = nullptr;
    fShadowBytes = fConvertedBytes = 0;
    fShadowValid = false;
    OSSafeReleaseNULL(fScanoutMap);
    fScanout = nullptr;
}

// Called by IOMobileFramebufferLegacy::start before it creates the default surface and registers the service (it tests bit 0).
bool NativeGOPDisplay::start_hardware()
{
    if (!createDisplayAttributes())
        return false;
    fVBL = IOTimerEventSource::timerEventSource(this, vblFired);
    if (!fVBL || getWorkLoop()->addEventSource(fVBL) != kIOReturnSuccess) {   // the work loop of the family's command gate
        IOLog("NativeGOPDisplay: Failed to create VBL timer\n");
        OSSafeReleaseNULL(fVBL);
        return false;
    }
    fVBL->enable();
    at<uint8_t>(this, kHardwareStarted) = 1;
    PMinit();
    getPlatform()->PMRegisterDevice(getProvider(), this);   // the provider is no power-managed AppleARMIODevice here
    registerPowerDriver(this, gPowerStates, 2);
    // The panel is always there: report it plugged in (the digital out state IOMobileFramebufferGetDigitalOutState answers), as
    // AppleParavirtDisplay does when the host reports its display. Unplugged (0), WindowServer kept this display disabled and composited
    // a virtual one as the main display ("get_hotplug_state - not active").
    hotPlug_notify(1);
    IOLog("NativeGOPDisplay: hardware started\n");
    return true;
}

IOReturn NativeGOPDisplay::stop_hardware_gated()
{
    if (fVBL) {
        fVBL->cancelTimeout();
        getWorkLoop()->removeEventSource(fVBL);
        OSSafeReleaseNULL(fVBL);
    }
    return kIOReturnSuccess;
}

void NativeGOPDisplay::vblFired(OSObject *owner, IOTimerEventSource *)
{
    static_cast<NativeGOPDisplay *>(owner)->swap_begin_gated();
}

// AppleParavirtDisplay::clean_queue_gated: whether a queued swap is ready to be programmed.
bool NativeGOPDisplay::clean_queue_gated()
{
    if (at<IOMFBSwapIORequest *>(this, kCurrentSwap) || !at<IOMFBSwapIORequest *>(this, kSwapQueueFirst))
        return false;
    if (dropObsoleteSwaps_gated())
        return false;
    dropNoOpSwaps_gated();
    dropAbortedSwaps_gated(false);
    dropDummySwaps_gated();
    return at<IOMFBSwapIORequest *>(this, kSwapQueueFirst) != nullptr;
}

// AppleParavirtDisplay::swap_begin_gated: the family calls it when a swap is submitted, the VBL timer every 16 ms.
IOReturn NativeGOPDisplay::swap_begin_gated()
{
    if (!fVBL)
        return kIOReturnNotReady;
    // Both submitted swaps and the timer enter here. Submit activity can
    // otherwise continually re-arm the 16ms timer and starve its callback.
    nativeGopVMKeepAwakeTick(this);
    fVBL->cancelTimeout();
    while (clean_queue_gated()) {
        IOMFBSwapIORequest *swap = at<IOMFBSwapIORequest *>(this, kSwapQueueFirst);
        at<IOMFBSwapIORequest *>(this, kCurrentSwap) = swap;
        iomfbUpdateSwapInfoState(this, swap, 2);
        IOMFBSwapIORequest *next = at<IOMFBSwapIORequest *>(swap, kSwapNext);
        IOMFBSwapIORequest **prev = at<IOMFBSwapIORequest **>(swap, kSwapPrev);
        if (next)
            at<IOMFBSwapIORequest **>(next, kSwapPrev) = prev;
        else
            at<IOMFBSwapIORequest **>(this, kSwapQueueLast) = prev;
        *prev = next;
        if ((at<uint32_t>(swap, kSwapDropReasons) & ~8u) == 0) {
            program_hardware_gated();
        } else {
            dropSwap_gated(swap, false);
            fDropped++;
        }
    }
    if (at<IOMFBSwapIORequest *>(this, kSwapQueueFirst))
        fStalled++;
    if (++fTicks % 625 == 0 && fSubmitted + fFrames + fDropped + fStalled != fReported) {   // about every 10 s while swaps arrive
        fReported = fSubmitted + fFrames + fDropped + fStalled;
        IOLog("NativeGOPDisplay: %llu swaps submitted, %llu shown, %llu dropped, %llu ticks stalled (current %p), last frame %llu lit centre %#llx, rows %llu written %llu unchanged\n",
              fSubmitted, fFrames, fDropped, fStalled, at<IOMFBSwapIORequest *>(this, kCurrentSwap), fLastLit, fLastCenter,
              fRowsWritten, fRowsSkipped);
    }
    return fVBL->setTimeoutMS(16);
}

// Diagnostic: CoreAnimation asks swap_wait, without blocking, whether its last swaps are done before it composes the next frame
// (IOMFBDisplay::can_update_); after three swaps WindowServer found the display unready for good.
IOReturn NativeGOPDisplay::swap_wait(IOMobileFramebufferUserClient *client, unsigned int swapID, unsigned int options, unsigned int timeout)
{
    IOReturn result = IOMobileFramebufferLegacy::swap_wait(client, swapID, options, timeout);
    if (result != kIOReturnSuccess && (++fWaitFailures <= 20 || fWaitFailures % 1000 == 0))
        IOLog("NativeGOPDisplay: swap_wait(%u, 0x%x, %u) = 0x%x (%llu failures): +6412 %u power %u/%u +6436 %u +140 0x%x current %p queue %p\n",
              swapID, options, timeout, result, fWaitFailures, at<uint32_t>(this, 6412), at<uint32_t>(this, kSystemPowerState),
              at<uint32_t>(this, kAPIPowerState), at<uint32_t>(this, kHardwarePowered), at<uint8_t>(this, kHardwareStarted),
              at<IOMFBSwapIORequest *>(this, kCurrentSwap), at<IOMFBSwapIORequest *>(this, kSwapQueueFirst));
    return result;
}

// AppleParavirtDisplay::do_power_state_change_gated: the display is on while both the system and WindowServer (setAPIPowerState)
// want it on; each change goes to WindowServer as a power state notification, which CoreAnimation waits for before it swaps.
IOReturn NativeGOPDisplay::do_power_state_change_gated()
{
    bool on = at<uint32_t>(this, kSystemPowerState) && at<uint32_t>(this, kAPIPowerState);
    if (on == fDisplayOn)
        return kIOReturnSuccess;
    fDisplayOn = on;
    at<uint32_t>(this, kHardwarePowered) = on;
    IOLog("NativeGOPDisplay: display %s\n", on ? "on" : "off");
    powerstate_notify(true, on);
    if (on) sheng_boot_checkpoint(7);
    return kIOReturnSuccess;
}

// Copy the compatible scene before completing the swap. Cursor-only updates
// leave scanout intact; no separate hardware cursor plane is advertised.
void NativeGOPDisplay::program_hardware_gated()
{
    IOMFBSwapIORequest *swap = at<IOMFBSwapIORequest *>(this, kCurrentSwap);
    uint8_t mask = at<uint8_t>(swap, kSwapLayers);
    IOSurface *surfaces[kNativeGopSwapLayerCount]{};
    NativeGopSwapLayer geometry[kNativeGopSwapLayerCount]{};
    uint64_t shape = mask;
    for (unsigned i = 0; i < kNativeGopSwapLayerCount; i++) {
        if (!(mask & (1u << i)))
            continue;
        surfaces[i] = at<IOSurface *>(swap, kSwapMainSurface + i * sizeof(IOSurface *));
        if (!surfaces[i])
            continue;
        geometry[i] = {ioSurfaceWidth(surfaces[i]), ioSurfaceHeight(surfaces[i]),
                       ioSurfacePixelFormat(surfaces[i])};
        shape = shape * 31 + geometry[i].width * 65537 + geometry[i].height;
    }
    int selected = nativeGopScanoutLayer(geometry, mask, fVideo.v_width, fVideo.v_height);
    if (shape != fLayerShape) {
        fLayerShape = shape;
        IOLog("NativeGOPDisplay: swap mask=%#x scanout layer=%d\n", mask, selected);
    }
    if (selected >= 0)
        copySurface(surfaces[selected]);
    swap_complete_gated();
}

static constexpr uint32_t kFormatBGRA = 'BGRA', kFormatRGhA = 'RGhA';   // 8-bit BGRA, 16-bit half-float RGBA

// A half float's value in [0, 1] as a byte, in integer arithmetic (no floating point in the kernel).
static uint8_t halfToByte(uint16_t half)
{
    unsigned exponent = (half >> 10) & 31, mantissa = half & 1023;
    if (half & 0x8000 || (exponent == 31 && mantissa))   // negative or NaN
        return 0;
    if (exponent >= 15)                                  // 1.0 and above
        return 255;
    uint32_t scaled = (exponent ? 1024 + mantissa : mantissa) * 255;   // value * 255 = scaled * 2^(exponent - 25), subnormals 2^-24
    unsigned shift = exponent ? 25 - exponent : 24;
    return static_cast<uint8_t>((scaled + (1u << (shift - 1))) >> shift);
}

// Cache read-only kernel mappings of the few root surfaces WindowServer rotates through, instead of
// preparing, mapping and unmapping a whole surface for every frame. Each cached descriptor is retained
// and prepared until evicted, so its pointer cannot be reused for another surface while cached.
const uint8_t *NativeGOPDisplay::mapSource(IOMemoryDescriptor *memory, uint64_t base, uint64_t span)
{
    SourceMap *victim = nullptr;
    for (SourceMap &entry : fSourceMaps) {
        if (entry.memory == memory && entry.base == base && entry.span == span) {
            entry.used = ++fSourceMapClock;
            return reinterpret_cast<const uint8_t *>(entry.map->getVirtualAddress());
        }
        if (!victim || !entry.memory || (victim->memory && entry.used < victim->used))
            victim = &entry;
    }
    if (victim->memory) {
        victim->map->release();
        victim->memory->complete();
        victim->memory->release();
        *victim = {};
    }
    if (memory->prepare() != kIOReturnSuccess)
        return nullptr;
    IOMemoryMap *map = memory->createMappingInTask(kernel_task, 0, kIOMapAnywhere | kIOMapReadOnly, base, span);
    if (!map || map->getLength() < span) {
        if (map)
            map->release();
        memory->complete();
        return nullptr;
    }
    memory->retain();
    *victim = {memory, map, base, span, ++fSourceMapClock};
    return reinterpret_cast<const uint8_t *>(map->getVirtualAddress());
}

void NativeGOPDisplay::releaseSourceMaps()
{
    for (SourceMap &entry : fSourceMaps) {
        if (!entry.memory)
            continue;
        entry.map->release();
        entry.memory->complete();
        entry.memory->release();
        entry = {};
    }
}

void NativeGOPDisplay::copySurface(IOSurface *surface)
{
    uint64_t width = ioSurfaceWidth(surface), height = ioSurfaceHeight(surface), rowBytes = ioSurfaceBytesPerRow(surface);
    uint64_t base = ioSurfaceBaseOffset(surface);
    uint32_t format = ioSurfacePixelFormat(surface);
    IOMemoryDescriptor *memory = ioSurfaceMemory(surface);
    if (format != fSurfaceFormat || width != fSourceWidth || height != fSourceHeight || !fFrames) {
        IOLog("NativeGOPDisplay: surface %llux%llu row %llu offset %llu format %#x memory %p\n", width, height, rowBytes, base, format, memory);
        fSurfaceFormat = format;
        fSourceWidth = width;
        fSourceHeight = height;
        fShadowValid = false;
    }
    uint64_t pixelBytes = format == kFormatRGhA ? 8 : 4;
    if (!memory || !fScanout || !width || !height || width > 16384 || height > 16384 ||
        rowBytes < width * pixelBytes || (format != kFormatBGRA && format != kFormatRGhA) ||
        base > memory->getLength() || rowBytes > (memory->getLength() - base) / height)
        return;
    const uint64_t copyWidth = fVideo.v_width, copyHeight = fVideo.v_height;
    const bool scaled = width != copyWidth || height != copyHeight;
    if (scaled && (width * copyHeight != height * copyWidth ||
                   width > copyWidth * 4 || height > copyHeight * 4 ||
                   copyWidth > width * 4 || copyHeight > height * 4)) {
        IOLog("NativeGOPDisplay: unsupported root surface geometry %llux%llu for %llux%llu\n",
              width, height, copyWidth, copyHeight);
        return;
    }
    if (format == kFormatRGhA && !fHalfToByte) {
        fHalfToByte = static_cast<uint8_t *>(IOMalloc(65536));
        if (!fHalfToByte) return;
        for (uint32_t half = 0; half < 65536; half++)
            fHalfToByte[half] = halfToByte(static_cast<uint16_t>(half));
    }
    const uint64_t sourceRowBytes = width * pixelBytes;
    if (fRowBytes < sourceRowBytes) {
        auto *replacement = static_cast<uint16_t *>(IOMalloc(sourceRowBytes));
        if (!replacement) return;
        if (fRow) IOFree(fRow, fRowBytes);
        fRow = replacement;
        fRowBytes = sourceRowBytes;
    }
    // The shadow mirrors the scanout this driver owns; it is sized once for the boot framebuffer.
    const uint64_t shadowBytes = copyWidth * copyHeight * 4, convertedBytes = copyWidth * 4;
    if (fShadowBytes != shadowBytes) {
        if (fShadow) IOFreeData(fShadow, fShadowBytes);
        fShadow = static_cast<uint32_t *>(IOMallocData(shadowBytes));
        fShadowBytes = fShadow ? shadowBytes : 0;
        fShadowValid = false;
        if (!fShadow) return;
    }
    if (fConvertedBytes != convertedBytes) {
        if (fConverted) IOFreeData(fConverted, fConvertedBytes);
        fConverted = static_cast<uint32_t *>(IOMallocData(convertedBytes));
        fConvertedBytes = fConverted ? convertedBytes : 0;
        if (!fConverted) return;
    }
    const uint64_t span = (height - 1) * rowBytes + sourceRowBytes;
    const uint8_t *source = span <= (512ULL << 20) ? mapSource(memory, base, span) : nullptr;
    // A frame without a cached mapping falls back to bounded row reads and rewrites every row.
    struct FallbackRead {
        IOMemoryDescriptor *memory = nullptr;
        ~FallbackRead() { if (memory) memory->complete(); }
    } fallback;
    if (!source) {
        if (memory->prepare() != kIOReturnSuccess)
            return;
        fallback.memory = memory;
        fShadowValid = false;
    }
    // The work loop serializes frames. Only bounded wired scanlines hold the pixel lease.
    struct DiagnosticHandoff {
        bool active = false, committed = false;
        uint64_t ready = 0, status = 0;
        explicit DiagnosticHandoff(bool first) {
            if (!first || !sheng_fb_claim_gui()) return;
            active = true;
            ready = sheng_panel_ready; status = sheng_status_pixels;
            sheng_panel_ready = 0; sheng_status_pixels = 0;
        }
        ~DiagnosticHandoff() {
            if (!active || committed) return;
            sheng_fb_abort_gui();
            uint64_t saved;
            if (sheng_fb_begin(SHENG_FB_BOOT, &saved)) {
                sheng_panel_ready = ready; sheng_status_pixels = status;
                sheng_fb_end(saved);
            }
        }
    } handoff(!fFrames);
    if (!fFrames && !handoff.active) return;
    if (fFrames % kFullRefreshFrames == 0)
        fShadowValid = false;
    uint64_t lit = 0, raw = 0, center = 0;
    // bring-up: half-float frames report lit pixels and the centre pixel of the last frame shown
    auto sampleRow = [&](uint64_t y, const uint16_t *row) {
        if (format != kFormatRGhA)
            return;
        for (uint64_t x = 0; x < copyWidth; x++) {
            const uint16_t *rgba = row + (x * width / copyWidth) * 4;
            lit += (fHalfToByte[rgba[0]] | fHalfToByte[rgba[1]] | fHalfToByte[rgba[2]]) != 0;
            raw += (rgba[0] | rgba[1] | rgba[2] | rgba[3]) != 0;
            if (y == copyHeight / 2 && x == copyWidth / 2)
                center = static_cast<uint64_t>(rgba[3]) << 48 | static_cast<uint64_t>(rgba[2]) << 32 | rgba[1] << 16 | rgba[0];
        }
    };
    struct GuiLease {
        uint64_t saved = 0;
        bool begin() { return sheng_fb_begin(SHENG_FB_GUI, &saved); }
        void end() { sheng_fb_end(saved); }
    } lease;
    NativeGopBlitResult blit{0, 0, true};
    if (source) {
        blit = nativeGopBlitRows(fScanout, fVideo.v_rowBytes, copyWidth, copyHeight, source, rowBytes, width, height,
                                 pixelBytes, format == kFormatRGhA ? fHalfToByte : nullptr, fRow, fConverted, fShadow,
                                 fShadowValid, lease, sampleRow);
    } else {
        for (uint64_t y = 0; y < copyHeight && blit.complete; y++) {
            const uint64_t sourceY = y * height / copyHeight;
            if (memory->readBytes(base + sourceY * rowBytes, fRow, sourceRowBytes) != sourceRowBytes) {
                IOLog("NativeGOPDisplay: short source row read\n");
                return;
            }
            sampleRow(y, fRow);
            if (!lease.begin()) {
                blit.complete = false;
                break;
            }
            nativeGopScanoutRow(reinterpret_cast<volatile uint32_t *>(fScanout + y * fVideo.v_rowBytes), copyWidth, fRow,
                                width, format == kFormatRGhA ? fHalfToByte : nullptr);
            lease.end();
            blit.written++;
        }
    }
    fRowsWritten += blit.written;
    fRowsSkipped += blit.skipped;
    if (!blit.complete)
        return;   // rows written so far match the shadow; an earlier invalid shadow stays invalid
    fShadowValid = source != nullptr;
    __asm__ volatile("dsb sy" ::: "memory");
    if (__atomic_load_n(&sheng_framebuffer.owner, __ATOMIC_ACQUIRE) != SHENG_FB_GUI) {
        fShadowValid = false;
        return;
    }
    fFrames++;
    handoff.committed = true;
    if (fFrames == 1) {
        PE_initialize_console(nullptr, kPEDisableScreen);
        sheng_boot_checkpoint(9);
    }
    fLastLit = lit;
    fLastCenter = center;
    if (format == kFormatRGhA && (fFrames <= 8 || fFrames % 50 == 0))
        IOLog("NativeGOPDisplay: frame %llu: %llu pixels lit, %llu nonzero, centre %#llx\n", fFrames, lit, raw, center);
}

static bool appendNumber(OSDictionary *dictionary, const char *key, uint64_t value, unsigned bits = 32)
{
    OSNumber *number = OSNumber::withNumber(value, bits);
    bool done = number && dictionary->setObject(key, number);
    OSSafeReleaseNULL(number);
    return done;
}

static bool appendString(OSDictionary *dictionary, const char *key, const char *value)
{
    OSString *string = OSString::withCString(value);
    bool done = string && dictionary->setObject(key, string);
    OSSafeReleaseNULL(string);
    return done;
}

static bool appendDictionary(OSDictionary *dictionary, const char *key, OSDictionary *value)
{
    bool done = value && dictionary->setObject(key, value);
    OSSafeReleaseNULL(value);
    return done;
}

static OSDictionary *point(uint32_t x, uint32_t y)
{
    OSDictionary *dictionary = OSDictionary::withCapacity(3);
    if (dictionary && !(appendNumber(dictionary, "X", x) && appendNumber(dictionary, "Y", y)))
        OSSafeReleaseNULL(dictionary);
    return dictionary;
}

// AppleParavirtDisplay::createDisplayAttributes, with the display of the boot framebuffer: one timing (the GOP mode) with one
// 8-bit SDR color element. Chromaticities are sRGB's (16.16 fixed point, as the panel's EDID would give them).
bool NativeGOPDisplay::createDisplayAttributes()
{
    uint32_t width = static_cast<uint32_t>(fVideo.v_width), height = static_cast<uint32_t>(fVideo.v_height);
    if (static_cast<uint64_t>(width + 160) * (height + 23) * kRefreshHz > UINT32_MAX)
        return false;
    bool done = false;
    OSDictionary *attributes = OSDictionary::withCapacity(16), *product = OSDictionary::withCapacity(8);
    OSDictionary *chromaticity = OSDictionary::withCapacity(3), *whitePoint = point(20493, 21561);
    OSDictionary *transport = OSDictionary::withCapacity(2), *sourceLimits = OSDictionary::withCapacity(5);
    OSDictionary *scalingLimits = OSDictionary::withCapacity(2);
    OSArray *colors = OSArray::withCapacity(1), *timings = OSArray::withCapacity(1), *timingColors = OSArray::withCapacity(1);
    // Default RGB advertises a native gamut and selects extended-color IOSurfaces.
    OSObject *color = ioavColorWithCapabilities(2, 8, 0, 0, 0, 10, false, nullptr, nullptr, nullptr, nullptr);
    // CVT reduced blanking as IOAVFamily's sCVTTimingData has it (1280x800@60: 1440x823, 71 MHz). Sync rates are 16.16 fixed point
    // (IOAVVideoTimingGetSyncRateRounded); the table leaves the horizontal one 0.
    uint8_t timingData[80] = {};
    uint32_t hTotal = width + 160, vTotal = height + 23;
    at<uint32_t>(timingData, 4) = hTotal;
    at<uint32_t>(timingData, 8) = width;
    at<uint32_t>(timingData, 12) = 32;                   // horizontal sync width, back porch, front porch
    at<uint32_t>(timingData, 16) = 80;
    at<uint32_t>(timingData, 20) = 48;
    at<uint32_t>(timingData, 28) = 1;                    // horizontal sync polarity (positive)
    at<uint32_t>(timingData, 36) = vTotal;
    at<uint32_t>(timingData, 40) = height;
    at<uint32_t>(timingData, 44) = 6;                    // vertical sync width, back porch, front porch
    at<uint32_t>(timingData, 48) = 14;
    at<uint32_t>(timingData, 52) = 3;
    at<uint32_t>(timingData, 56) = kRefreshHz << 16;
    at<uint32_t>(timingData, 68) = hTotal * vTotal * kRefreshHz;   // pixel clock in Hz
    OSObject *timing = ioavTimingWithData(timingData, 2, 4);
    do {
        if (!attributes || !product || !chromaticity || !whitePoint || !transport || !sourceLimits || !scalingLimits || !colors ||
            !timings || !timingColors || !color || !timing)
            break;
        at<uint64_t>(color, kElementID) = kColorID;
        at<uint32_t>(color, kElementScore) = kColorID;
        at<uint64_t>(timing, kElementID) = kTimingID;
        at<uint32_t>(timing, kElementScore) = kTimingID;
        if (!colors->setObject(color) || !timingColors->setObject(color) || !timings->setObject(timing))
            break;
        ioavTimingSetColorElements(timing, timingColors);
        if (!(appendString(product, "ManufacturerID", "UNK") && appendNumber(product, "ProductID", 0) &&
              appendNumber(product, "SerialNumber", 0) && appendString(product, "ProductName", "Sheng Boot Framebuffer") &&
              appendNumber(product, "WeekOfManufacture", 24) && appendNumber(product, "YearOfManufacture", 2021)))
            break;
        if (!(appendDictionary(chromaticity, "Red", point(41943, 21627)) && appendDictionary(chromaticity, "Green", point(19661, 39322)) &&
              appendDictionary(chromaticity, "Blue", point(9830, 3932))))
            break;
        if (!appendNumber(whitePoint, "Gamma", 144179))   // 2.2
            break;
        if (!(attributes->setObject("SupportsPQEOTF", kOSBooleanFalse) && attributes->setObject("ProductAttributes", product) &&
              attributes->setObject("Chromaticity", chromaticity) && attributes->setObject("DefaultColorSpaceIsSRGB", kOSBooleanFalse) &&
              attributes->setObject("DefaultWhitePoint", whitePoint) && attributes->setObject("SupportsStandby", kOSBooleanTrue) &&
              appendNumber(attributes, "ContinuousFrequencySupport", 3) &&
              appendNumber(attributes, "MaxHorizontalImageSize", (width * 254 + 4800) / 9600) &&
              appendNumber(attributes, "MaxVerticalImageSize", (height * 254 + 4800) / 9600) &&
              appendNumber(attributes, "NativeFormatHorizontalPixels", width) && appendNumber(attributes, "NativeFormatVerticalPixels", height) &&
              appendNumber(attributes, "PreciseAspectRatio", (static_cast<uint64_t>(width) << 16) / height)))
            break;
        if (!(appendString(transport, "Upstream", "Other") && appendString(transport, "Downstream", "Other")))
            break;
        if (!(appendNumber(sourceLimits, "MaxSrcBufferWidth", 16384) && appendNumber(sourceLimits, "MaxSrcBufferHeight", 16384) &&
              appendNumber(sourceLimits, "MaxSrcRectWidth", 8192) && appendNumber(sourceLimits, "MaxSrcRectTotal", 0) &&
              appendNumber(sourceLimits, "IOMFBMaxCompressedSizeInBytes", 0)))
            break;
        if (!(appendNumber(scalingLimits, "RGBLayer_MinScaleFraction", 4) && appendNumber(scalingLimits, "RGBLayer_MaxScale", 4)))
            break;
        setProperty("IOMFBUUID", "5348454E-472D-474F-5000-000000000001");
        setProperty("external", false);
        setProperty("primary", 0ULL, 32);   // AppleParavirtDisplay's display index
        done = setProperty("DisplayAttributes", attributes) && setProperty("Transport", transport) &&
            setProperty("ColorElements", colors) && setProperty("TimingElements", timings) &&
            setProperty("IOMFBMaxSrcPixels", sourceLimits) && setProperty("IOMFBScalingLimits", scalingLimits);
    } while (false);
    OSSafeReleaseNULL(attributes);
    OSSafeReleaseNULL(product);
    OSSafeReleaseNULL(chromaticity);
    OSSafeReleaseNULL(whitePoint);
    OSSafeReleaseNULL(transport);
    OSSafeReleaseNULL(sourceLimits);
    OSSafeReleaseNULL(scalingLimits);
    OSSafeReleaseNULL(colors);
    OSSafeReleaseNULL(timings);
    OSSafeReleaseNULL(timingColors);
    OSSafeReleaseNULL(color);
    OSSafeReleaseNULL(timing);
    if (!done || set_digital_out_mode(kColorID, kTimingID) != kIOReturnSuccess) {
        IOLog("NativeGOPDisplay: createDisplayAttributes failed\n");
        return false;
    }
    return true;
}

// The mode of a color and a timing element (the IDs published above); AppleParavirtDisplay's block takes the timing's active size.
IOReturn NativeGOPDisplay::set_digital_out_mode(unsigned int colorID, unsigned int timingID)
{
    if (colorID != kColorID || timingID != kTimingID)
        return kIOReturnBadArgument;
    at<uint32_t>(this, kDisplayWidth) = static_cast<uint32_t>(fVideo.v_width);
    at<uint32_t>(this, kDisplayHeight) = static_cast<uint32_t>(fVideo.v_height);
    setProperty("DisplayWidth", fVideo.v_width, 32);
    setProperty("DisplayHeight", fVideo.v_height, 32);
    setProperty("DPTimingModeId", timingID, 32);
    return kIOReturnSuccess;
}

// GOP supplies no EDID here; use a 96-dpi logical fallback.
IOReturn NativeGOPDisplay::get_dot_pitch(unsigned int *pitch)
{
    *pitch = 96;
    return kIOReturnSuccess;
}

// AppleParavirtDisplay's hooks with nothing to do on a CPU-copied scanout: the swap and its layers need no mapping, there is no
// gamma or color hardware (the family's defaults are unsupported, AppleParavirtDisplay accepts them).
IOReturn NativeGOPDisplay::verify_swap(IOMFBSwapIORequest *)   // AppleParavirtDisplay returns 1 (IOMobileFramebufferLegacy: 0)
{
    if (!fSubmitted++) {
        sheng_boot_checkpoint(8);
        IOLog("NativeGOPDisplay: first swap submitted\n");
    }
    return 1;
}
IOReturn NativeGOPDisplay::swap_layer_map(IOMFBSwapIORequest *, unsigned int) {return kIOReturnSuccess;}
IOReturn NativeGOPDisplay::surface_layer_map(IOMFB::AP::DisplayedData *, unsigned int, IOSurface *) {return kIOReturnSuccess;}
IOReturn NativeGOPDisplay::surface_layer_unmap(IOMFB::AP::DisplayedData *, unsigned int) {return kIOReturnSuccess;}
IOReturn NativeGOPDisplay::set_display_device(unsigned int) {return kIOReturnSuccess;}
IOReturn NativeGOPDisplay::get_gamma_table(IOMFBGammaTable *) {return kIOReturnUnsupported;}
IOReturn NativeGOPDisplay::set_gamma_table(IOMFBGammaTable *) {return kIOReturnSuccess;}
IOReturn NativeGOPDisplay::set_color_remap_mode(DisplayColorRemapMode mode)
{
    fColorRemapMode = mode;
    return kIOReturnSuccess;
}
IOReturn NativeGOPDisplay::get_color_remap_mode(DisplayColorRemapMode *mode) const
{
    *mode = fColorRemapMode;
    return kIOReturnSuccess;
}
IOReturn NativeGOPDisplay::swap_set_color_matrix(IOMFBColorFixedMatrix *, IOMFBColorMatrixFunction, unsigned int) {return kIOReturnSuccess;}

// The slots IOMobileFramebufferLegacy leaves pure, as AppleParavirtDisplay implements them.
IOReturn NativeGOPDisplay::set_notify_tag_mask_gated(IOMFB_NotificationType type, IOMFBNotifyRequest *request, unsigned int mask)
{
    if (type != 3)
        return kIOReturnBadArgument;
    at<uint32_t>(request, 120) = mask;
    return kIOReturnSuccess;
}
IOReturn NativeGOPDisplay::supported_frame_info(unsigned int, unsigned int *, char (*)[64], unsigned int *) {return kIOReturnUnsupported;}
IOReturn NativeGOPDisplay::factory_portal(void *, task *) {return kIOReturnUnsupported;}
IOReturn NativeGOPDisplay::executeFactoryConfigurations_gated() {return kIOReturnUnsupported;}
IOReturn NativeGOPDisplay::set_frame_info_notification_state_gated(IOMFBNotifyRequest *, bool) {return kIOReturnSuccess;}

// AppleParavirtDisplay::notify_swap_done_gated: the family calls it as the current swap completes; the swap info goes to the
// clients (swap_info_notify_dispatch_gated, which calls do_swap_info_notify_gated per request) unless notifications are held.
IOReturn NativeGOPDisplay::notify_swap_done_gated(unsigned int swapID, ::SwapInfoBlob *, unsigned int *, unsigned int)
{
    IOMFBSwapIORequest *swap = at<IOMFBSwapIORequest *>(this, kCurrentSwap);
    if (at<uint32_t>(swap, kSwapDropReasons))
        return kIOReturnUnsupported;
    SwapInfo *info = &fSwapInfo[fSwapInfoWrite];
    *info = {swapID, {at<uint64_t>(this, kSwapInfoTime), 0, at<uint64_t>(swap, kSwapInfoA), at<uint32_t>(swap, kSwapInfoB), 0}};
    if (at<uint16_t>(this, kSwapInfoHeld)) {
        fSwapInfoWrite = (fSwapInfoWrite + 1) % kSwapInfoRing;
        return kIOReturnSuccess;
    }
    for (; fSwapInfoRead != fSwapInfoWrite; fSwapInfoRead = (fSwapInfoRead + 1) % kSwapInfoRing)
        iomfbSwapInfoDispatch(this, &fSwapInfo[fSwapInfoRead]);
    iomfbSwapInfoDispatch(this, info);
    return kIOReturnSuccess;
}

// AppleParavirtDisplay::do_swap_info_notify_gated: the request's async result is the mask, the swap ID and the words it asks for.
IOReturn NativeGOPDisplay::do_swap_info_notify_gated(IOMFBNotifyRequest *request, const SwapInfoBlob *blob)
{
    const SwapInfo *info = reinterpret_cast<const SwapInfo *>(blob);
    uint32_t mask = at<uint32_t>(request, kNotifyMask);
    io_user_reference_t args[12] = {mask, info->swapID};
    unsigned count = 2;
    for (unsigned word = 0; word < 5; word++) {
        if (!(mask >> word & 1))
            continue;
        uint64_t value = word == 1 ? info->words[0] - at<uint64_t>(request, kNotifyLastTime) : info->words[word];
        unsigned need = mask >> 31 ? 2 : 1;
        if (count + need > 12) {
            IOLog("IOMFB Packer put: ran out of space\n");
            continue;
        }
        if (need == 2) {
            args[count++] = static_cast<uint32_t>(value);
            args[count++] = value >> 32;
        } else {
            args[count++] = value;
        }
    }
    at<uint64_t>(request, kNotifyLastTime) = info->words[0];
    return userClientSendAsyncResult(&at<io_user_reference_t>(request, kNotifyAsyncReference), kIOReturnSuccess, args, count,
                                     kIOUserNotifyOptionCanDrop);
}
IOReturn NativeGOPDisplay::notify_crc_ready_gated(unsigned int) {return kIOReturnSuccess;}
bool NativeGOPDisplay::are_timings_enabled() const {return false;}
