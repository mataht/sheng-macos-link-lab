// SPDX-License-Identifier: GPL-3.0-or-later
// NativeTouch: macOS 26 front end for the NT36532E touch bring-up of UniXNU-sheng (package/TouchProbe.cpp, with the
// GENI SPI, Novatek protocol and THP touch-core libraries). The bring-up runs the controller; this file supplies the
// vendor firmware from the device tree and turns the published finger set into a single-touch absolute pointer.
//
// Default off: the upstream stage argument sheng-touch=<1..4> (1 read-only snapshot, 2 clocks and pins, 3 GENI SPI and
// chip id, 4 firmware download and frames); hardware only (PE_vmm_present == 0). Optional native-touch-orient=<bits>
// (touch_pointer.h). The firmware is never compiled in: a local packager adds /arm-io/native-touch with the property
// "firmware", accepted only when its size and SHA-256 equal the pinned capture (touch_firmware_sha256.h). Without it the
// bring-up stops before the download stage, as upstream does without its generated firmware object.
#include <IOKit/IOBufferMemoryDescriptor.h>
#include <IOKit/IOLib.h>
#include <IOKit/IOService.h>
#include <IOKit/IOUserClient.h>
#include <IOKit/IODeviceTreeSupport.h>
#include <IOKit/hid/IOHIDDevice.h>
#include <libkern/c++/OSData.h>
#include <kern/thread.h>
#include <pexpert/pexpert.h>
#include <string.h>
#include "touch_firmware_sha256.h"
#include "touch_pointer.h"

static_assert(sizeof(IOHIDDevice) == 208, "paired 25F71 IOHIDDevice size");

extern "C" {
void native_touch_probe(void);
void native_touch_set_sink(void (*sink)(void *), void *context);
extern volatile uint32_t native_touch_seq, native_touch_count;
extern volatile int native_touch_xs[12], native_touch_ys[12], native_touch_ids[12];
extern char native_touch_rows[4][120];
extern volatile uint32_t native_touch_stage_done, native_touch_step;
extern volatile int native_touch_error;
volatile uint32_t native_touch_firmware_state;   // 0 none, 1 accepted, 2 rejected (size or SHA-256)
volatile uint32_t native_touch_pointer_reports;
// Input counters shown by the display companion's native-gop-diag strip (defined there, zero until the first touch):
// 0 reports, 1 finger-down count, 2 finger-up count, 3 last X, 4 last Y, 5 HID user clients, 6 handleReport failures.
extern volatile uint32_t sheng_touch_diag[8];
}

static inline void touchDiagBump(unsigned index) { sheng_touch_diag[index] = sheng_touch_diag[index] + 1; }

namespace {
const uint8_t *gFirmware;
uint32_t gFirmwareSize;

// The bring-up keeps its state (stage, step, error, clock votes, pin configuration, chip id, transfer status) in
// native_touch_rows; upstream draws them on its own diagnostic page. Print them on the kernel console whenever the
// stage, step or error changes, and every 10 s, for the first 60 s, so a photo of the boot text records them.
// Read-only: copies the rows and logs them, never touches the hardware.
void touchReportThread(void *, wait_result_t)
{
  uint32_t lastDone = ~0u, lastStep = ~0u;
  int lastError = 0x7fffffff;
  char row[sizeof native_touch_rows[0]];
  for (unsigned tick = 0; tick <= 600; ++tick) {
    const uint32_t done = native_touch_stage_done, step = native_touch_step;
    const int error = native_touch_error;
    if (done != lastDone || step != lastStep || error != lastError || tick % 100 == 0) {
      lastDone = done;
      lastStep = step;
      lastError = error;
      for (unsigned i = 0; i < 4; ++i) {
        memcpy(row, native_touch_rows[i], sizeof row);
        row[sizeof row - 1] = 0;
        IOLog("NativeTouch row%u t%us: %s\n", i, tick / 10, row);
      }
    }
    IOSleep(100);
  }
}
}

// Called by the patched bring-up before the download stage.
extern "C" const uint8_t *native_touch_firmware_bytes(uint32_t *size)
{
  if (size) *size = gFirmware ? gFirmwareSize : 0;
  return gFirmware;
}

class NativeTouchHID : public IOHIDDevice {
  OSDeclareDefaultStructors(NativeTouchHID)

public:
  IOReturn newReportDescriptor(IOMemoryDescriptor **descriptor) const override
  {
    *descriptor = IOBufferMemoryDescriptor::withBytes(sheng_touch::kPointerDescriptor, sizeof(sheng_touch::kPointerDescriptor),
                                                      kIODirectionNone);
    return *descriptor ? kIOReturnSuccess : kIOReturnNoMemory;
  }
  OSString *newTransportString() const override { return OSString::withCString("SPI"); }
  OSString *newManufacturerString() const override { return OSString::withCString("Novatek"); }
  OSString *newProductString() const override { return OSString::withCString("Sheng Touch Screen"); }
  OSNumber *newVendorIDNumber() const override { return OSNumber::withNumber(0x1209, 32); }
  OSNumber *newProductIDNumber() const override { return OSNumber::withNumber(0x5354, 32); }
  OSNumber *newLocationIDNumber() const override { return OSNumber::withNumber(0x4e543336, 32); }
  OSNumber *newPrimaryUsagePageNumber() const override { return OSNumber::withNumber(kHIDPage_GenericDesktop, 32); }
  OSNumber *newPrimaryUsageNumber() const override { return OSNumber::withNumber(kHIDUsage_GD_Mouse, 32); }
};

OSDefineMetaClassAndStructors(NativeTouchHID, IOHIDDevice)

class NativeTouch : public IOService {
  OSDeclareDefaultStructors(NativeTouch)

  NativeTouchHID *fHid = nullptr;
  sheng_touch::Pointer fPointer;
  sheng_touch::Orientation fOrientation;

  static void sink(void *self);
  static void diagThread(void *self, wait_result_t);
  void loadFirmware(uint32_t &orientBits);
  unsigned countUserClients();

public:
  bool start(IOService *provider) override;
};

OSDefineMetaClassAndStructors(NativeTouch, IOService)

void NativeTouch::loadFirmware(uint32_t &orientBits)
{
  IORegistryEntry *node = IORegistryEntry::fromPath("/arm-io/native-touch", gIODTPlane);
  if (!node) {
    IOLog("NativeTouch: no /arm-io/native-touch node; the bring-up will stop before the firmware download\n");
    return;
  }
  OSData *firmware = OSDynamicCast(OSData, node->getProperty("firmware"));
  if (firmware && sheng::touch_firmware::accepted(static_cast<const uint8_t *>(firmware->getBytesNoCopy()), firmware->getLength())) {
    uint8_t *copy = static_cast<uint8_t *>(IOMalloc(firmware->getLength()));
    if (copy) {
      memcpy(copy, firmware->getBytesNoCopy(), firmware->getLength());
      gFirmwareSize = firmware->getLength();
      gFirmware = copy;
      native_touch_firmware_state = 1;
    }
  } else {
    native_touch_firmware_state = 2;
  }
  static const struct { const char *key; uint32_t bit; } flips[] = {{"flip-x", 2u}, {"flip-y", 4u}};
  for (const auto &flip : flips) {
    OSData *flag = OSDynamicCast(OSData, node->getProperty(flip.key));
    if (flag && flag->getLength() == 4 && *static_cast<const uint32_t *>(flag->getBytesNoCopy()))
      orientBits |= flip.bit;
  }
  IOLog("NativeTouch: firmware %s (%u bytes)\n", gFirmware ? "accepted" : "absent or rejected", gFirmwareSize);
  node->release();
}

bool NativeTouch::start(IOService *provider)
{
  uint32_t stage = 0;
  if (!PE_parse_boot_argn("sheng-touch", &stage, sizeof(stage)) || stage < 1 || stage > 4)
    return false;
  if (PE_vmm_present != 0) {
    IOLog("NativeTouch: the touch controller is not driven in a VM\n");
    return false;
  }
  if (!IOService::start(provider))
    return false;
  uint32_t orientBits = 0;
  loadFirmware(orientBits);
  uint32_t argument = 0;
  if (PE_parse_boot_argn("native-touch-orient", &argument, sizeof(argument)))
    orientBits = argument & 7;
  fOrientation = sheng_touch::orientation(orientBits);
  fHid = OSTypeAlloc(NativeTouchHID);
  if (!fHid || !fHid->init(nullptr) || !fHid->attach(this)) {
    OSSafeReleaseNULL(fHid);
  } else if (!fHid->start(this)) {
    fHid->detach(this);
    OSSafeReleaseNULL(fHid);
  }
  if (!fHid) {
    IOLog("NativeTouch: cannot start the HID device\n");
    return false;
  }
  retain();   // the bring-up thread keeps calling sink() for the life of the system
  native_touch_set_sink(sink, this);
  native_touch_probe();
  registerService();
  IOLog("NativeTouch: bring-up stage %u started, orientation bits %u\n", stage, orientBits);
  thread_t reporter = nullptr;
  if (kernel_thread_start(touchReportThread, nullptr, &reporter) == KERN_SUCCESS)
    thread_deallocate(reporter);
  else
    IOLog("NativeTouch: cannot start the diagnostic reporter\n");
  // native-touch-diag=1 keeps counting who has opened the HID service (hidd, WindowServer) for the display's diagnostic strip.
  unsigned diag = 0;
  if (PE_parse_boot_argn("native-touch-diag", &diag, sizeof(diag)) && diag == 1 &&
      kernel_thread_start(diagThread, this, &reporter) == KERN_SUCCESS)
    thread_deallocate(reporter);
  return true;
}

unsigned NativeTouch::countUserClients()
{
  unsigned clients = 0;
  OSIterator *all = IORegistryIterator::iterateOver(fHid, gIOServicePlane, kIORegistryIterateRecursively);
  if (all) {
    while (OSObject *entry = all->getNextObject())
      clients += OSDynamicCast(IOUserClient, entry) != nullptr;
    all->release();
  }
  return clients;
}

void NativeTouch::diagThread(void *self, wait_result_t)
{
  NativeTouch *touch = static_cast<NativeTouch *>(self);
  for (;;) {
    sheng_touch_diag[5] = touch->countUserClients();
    IOSleep(5000);
  }
}

void NativeTouch::sink(void *self)
{
  NativeTouch *touch = static_cast<NativeTouch *>(self);
  sheng_touch::Finger fingers[sheng_touch::kSlots];
  unsigned count = 0;
  for (unsigned attempt = 0; attempt < 8; ++attempt) {
    const uint32_t before = native_touch_seq;
    if (before & 1) continue;
    __asm__ volatile("dmb ish" ::: "memory");
    count = native_touch_count < sheng_touch::kSlots ? native_touch_count : sheng_touch::kSlots;
    for (unsigned i = 0; i < count; ++i)
      fingers[i] = sheng_touch::Finger{native_touch_ids[i], native_touch_xs[i], native_touch_ys[i]};
    __asm__ volatile("dmb ish" ::: "memory");
    if (native_touch_seq == before) break;
    count = 0;
  }
  uint8_t bytes[sheng_touch::kReportBytes];
  const bool wasDown = touch->fPointer.down;
  if (!sheng_touch::update(touch->fPointer, fingers, count, touch->fOrientation, bytes))
    return;
  if (touch->fPointer.down != wasDown)
    touchDiagBump(touch->fPointer.down ? 1 : 2);
  sheng_touch_diag[3] = touch->fPointer.x;
  sheng_touch_diag[4] = touch->fPointer.y;
  IOBufferMemoryDescriptor *report = IOBufferMemoryDescriptor::withBytes(bytes, sizeof(bytes), kIODirectionNone);
  if (report) {
    if (touch->fHid->handleReport(report) != kIOReturnSuccess)
      touchDiagBump(6);
    report->release();
    native_touch_pointer_reports = native_touch_pointer_reports + 1;
    touchDiagBump(0);
  }
}
