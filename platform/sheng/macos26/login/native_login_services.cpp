// SPDX-License-Identifier: BSD-2-Clause-Patent
// Two services the macOS login path looks up and this kernel otherwise lacks. Both are default off and take a boot argument.
//
// AppleKeyStore (sheng-aks=<1,2>): registered as IOService:/IOResources/AppleKeyStore, the path libaks opens. It answers
// the state queries truthfully for a system without Secure Enclave: no keybag, no passcode, nothing locked. Stage 2 adds
// the software key wrap of the upstream driver. It protects nothing and must not be taken for secure storage. Derived
// from UniXNU/AppleKeyStoreState (BSD-2-Clause-Patent; see aks_protocol.h for the upstream commit).
//
// IOWatchdog (sheng-watchdog=1): the class the watchdogd launch event matches. The real service lives in
// AppleARMWatchdogTimer and needs the SoC watchdog; this one reports monitoring disabled, so watchdogd exits 0 instead
// of stopping on its missing-kext assertion (a SIGTRAP, which panicked hardware boot 01 through _PanicOnCrash).
//
// These two run in QEMU as well as on the tablet, because guest logs are readable in QEMU: the gate is the boot
// argument, not PE_vmm_present.
#include <IOKit/IOService.h>
#include <IOKit/IOUserClient.h>
#include <IOKit/IOPlatformExpert.h>
#include <IOKit/IOLib.h>
#include <IOKit/IOMemoryDescriptor.h>
#include <IOKit/IOKitKeys.h>
#include <libkern/c++/OSData.h>
#include <libkern/c++/OSString.h>
#include <pexpert/pexpert.h>
#include <stdint.h>
#include <string.h>

#include "aks_protocol.h"

namespace {
constexpr IOReturn kAksError = (IOReturn)0xe00002bc;  // what libaks reports itself when the connection is missing
constexpr IOReturn kAksUnsupported = (IOReturn)0xe00002c7;
constexpr uint32_t kLogSelectors = 192;

uint32_t aks_stage(void) {
  static int cached = -1;
  if (cached < 0) {
    uint32_t value = 0;
    if (!PE_parse_boot_argn("sheng-aks", &value, sizeof(value)) || value < 1 || value > sheng_aks::kMaxStage) value = 0;
    cached = (int)value;
  }
  return (uint32_t)cached;
}

// sheng-aks-sls=<n> overrides the reported lock state (default 0 = unlocked; 3 = MobileKeyBag's "no passcode").
uint32_t aks_lock_state(void) {
  static int cached = -1;
  if (cached < 0) {
    uint32_t value = 0;
    if (!PE_parse_boot_argn("sheng-aks-sls", &value, sizeof(value)) || value > 7) value = 0;
    cached = (int)value;
  }
  return (uint32_t)cached;
}

// The platform identity (IOPlatformUUID) keys the software wrap, so items wrapped in one boot open in the next.
size_t platform_identity(uint8_t *out, size_t capacity) {
  IOPlatformExpert *platform = IOService::getPlatform();
  IORegistryEntry *device = platform ? (IORegistryEntry *)platform->getProvider() : nullptr;
  if (!device) return 0;
  OSObject *value = device->copyProperty(kIOPlatformUUIDKey);
  size_t n = 0;
  if (OSString *string = OSDynamicCast(OSString, value)) {
    for (const char *p = string->getCStringNoCopy(); p && *p && n < capacity; ++p) out[n++] = (uint8_t)*p;
  } else if (OSData *data = OSDynamicCast(OSData, value)) {
    n = data->getLength() < capacity ? data->getLength() : capacity;
    memcpy(out, data->getBytesNoCopy(), n);
  }
  OSSafeReleaseNULL(value);
  return n;
}

bool device_key(uint8_t key[32]) {
  static uint8_t cached[32];
  static volatile bool ready;
  if (!ready) {
    uint8_t id[64];
    size_t n = platform_identity(id, sizeof id);
    if (!n) return false;
    sheng_aks::device_key(id, n, cached);
    ready = true;
  }
  memcpy(key, cached, sizeof cached);
  return true;
}

IOReturn write_output(IOExternalMethodArguments *args, const uint8_t *data, size_t n) {
  if (args->structureOutputDescriptor) {
    if (n > args->structureOutputDescriptor->getLength()) return kIOReturnNoSpace;
    args->structureOutputDescriptor->writeBytes(0, data, n);
    args->structureOutputDescriptorSize = (uint32_t)n;
    return kIOReturnSuccess;
  }
  if (!args->structureOutput || n > args->structureOutputSize) return kIOReturnNoSpace;
  memcpy(args->structureOutput, data, n);
  args->structureOutputSize = (uint32_t)n;
  return kIOReturnSuccess;
}

// Every selector is logged the first time it is seen, with its result: this is what the QEMU runs and the boot text use
// to learn which selectors macOS actually needs. Unknown selectors are logged as often as they are not repeated.
volatile uint32_t aks_call_count[kLogSelectors];
void aks_note(const char *service, uint32_t selector, IOReturn rc) {
  if (selector < kLogSelectors) {
    uint32_t n = __atomic_add_fetch(&aks_call_count[selector], 1, __ATOMIC_RELAXED);
    if (n == 1 || (rc != kIOReturnSuccess && n % 100 == 1))
      IOLog("%s: selector %u first answered 0x%x (count %u)\n", service, selector, rc, n);
  } else {
    IOLog("%s: selector %u answered 0x%x\n", service, selector, rc);
  }
}

IOReturn aks_dispatch(uint32_t selector, IOExternalMethodArguments *args) {
  using namespace sheng_aks;
  if (!args) return kAksError;
  switch (action_for_selector(selector, aks_stage())) {
    case Action::Unsupported:
      return kAksUnsupported;
    case Action::Success:
      args->scalarOutputCount = 0;
      return kIOReturnSuccess;
    case Action::System:
      if (args->scalarOutputCount < 1 || !args->scalarOutput) return kIOReturnBadArgument;
      args->scalarOutput[0] = kSystemHandle;
      args->scalarOutputCount = 1;
      return kIOReturnSuccess;
    case Action::LockState:
      if (args->scalarInputCount != 1 || args->scalarOutputCount != 1 || !args->scalarOutput) return kIOReturnBadArgument;
      args->scalarOutput[0] = kUnlockedSinceBoot;
      return kIOReturnSuccess;
    case Action::DeviceState: {
      if (args->scalarInputCount > 1 || args->scalarOutputCount != 0) return kIOReturnBadArgument;
      uint8_t der[192];
      size_t n = encode_device_state(der, sizeof der, aks_lock_state());
      if (!n) return kIOReturnNoSpace;
      return write_output(args, der, n);
    }
    case Action::Generation:
      if (args->scalarInputCount != 2 || !args->scalarOutput || args->scalarOutputCount != 1) return kIOReturnBadArgument;
      args->scalarOutput[0] = 0;
      return kIOReturnSuccess;
    case Action::Wrap: {
      size_t n = args->structureInputSize;
      if (args->scalarInputCount != 2 || !args->scalarOutput || args->scalarOutputCount != 1 || !args->structureInput ||
          !n || n > kMaxKeyBytes) return kIOReturnBadArgument;
      uint8_t key[32], blob[kTagBytes + kMaxKeyBytes];
      if (!device_key(key)) return kAksError;
      wrap_key(key, (const uint8_t *)args->structureInput, n, blob);
      IOReturn rc = write_output(args, blob, kTagBytes + n);
      if (rc == kIOReturnSuccess) args->scalarOutput[0] = args->scalarInput[1];
      return rc;
    }
    case Action::Unwrap: {
      size_t n = args->structureInputSize;
      if (args->scalarInputCount != 2 || args->scalarOutputCount != 0 || !args->structureInput ||
          n <= kTagBytes || n > kTagBytes + kMaxKeyBytes) return kIOReturnBadArgument;
      uint8_t key[32], plain[kMaxKeyBytes];
      if (!device_key(key)) return kAksError;
      if (!unwrap_key(key, (const uint8_t *)args->structureInput, n, plain)) return kAksError;
      return write_output(args, plain, n - kTagBytes);
    }
    case Action::BagUuid: {
      uint8_t key[32], uuid[16];
      if (!device_key(key)) return kAksError;
      bag_uuid(key, uuid);
      args->scalarOutputCount = 0;
      return write_output(args, uuid, sizeof uuid);
    }
  }
  return kAksError;
}

IOReturn watchdog_dispatch(uint32_t selector, IOExternalMethodArguments *args) {
  using namespace sheng_watchdog;
  if (!args) return kIOReturnBadArgument;
  switch (action_for_selector(selector)) {
    case Action::Unsupported:
      return kIOReturnUnsupported;
    case Action::ReportDisabled:
    case Action::ReportZero:
      if (args->scalarOutputCount != 1 || !args->scalarOutput) return kIOReturnBadArgument;
      args->scalarOutput[0] = 0;
      return kIOReturnSuccess;
    case Action::Accept:
      args->scalarOutputCount = 0;
      return kIOReturnSuccess;
    case Action::ReportIOKitOff: {
      static const uint8_t off[2] = {0, 0};
      return write_output(args, off, sizeof off);
    }
  }
  return kIOReturnUnsupported;
}
}  // namespace

class AppleKeyStoreUserClient final : public IOUserClient {
  OSDeclareDefaultStructors(AppleKeyStoreUserClient)

public:
  IOReturn clientClose() override { terminate(); return kIOReturnSuccess; }
  IOReturn externalMethod(uint32_t selector, IOExternalMethodArguments *args, IOExternalMethodDispatch *, OSObject *,
                          void *) override {
    IOReturn rc = aks_dispatch(selector, args);
    aks_note("NativeAKS", selector, rc);
    return rc;
  }
};
OSDefineMetaClassAndStructors(AppleKeyStoreUserClient, IOUserClient)

class AppleKeyStore final : public IOService {
  OSDeclareDefaultStructors(AppleKeyStore)
  IOReturn open(task_t task, void *security, UInt32 type, OSDictionary *properties, IOUserClient **out) {
    if (!out) return kIOReturnBadArgument;
    *out = nullptr;
    AppleKeyStoreUserClient *client = OSTypeAlloc(AppleKeyStoreUserClient);
    if (!client) return kIOReturnNoMemory;
    if (!client->initWithTask(task, security, type, properties) || !client->attach(this)) {
      client->release();
      return kIOReturnError;
    }
    if (!client->start(this)) {
      client->detach(this);
      client->release();
      return kIOReturnError;
    }
    *out = client;
    return kIOReturnSuccess;
  }

public:
  bool start(IOService *provider) override {
    if (aks_stage() == 0 || !IOService::start(provider)) return false;
    setProperty("NativeKeyStoreEvents", true);
    setProperty("KeyOperationsAvailable", aks_stage() >= 2);
    registerService();
    IOLog("NativeAKS: stage %u started\n", aks_stage());
    return true;
  }
  IOReturn newUserClient(task_t task, void *security, UInt32 type, OSDictionary *properties, IOUserClient **out) override {
    return open(task, security, type, properties, out);
  }
  IOReturn newUserClient(task_t task, void *security, UInt32 type, IOUserClient **out) override {
    return open(task, security, type, nullptr, out);
  }
};
OSDefineMetaClassAndStructors(AppleKeyStore, IOService)

class IOWatchdogUserClient final : public IOUserClient {
  OSDeclareDefaultStructors(IOWatchdogUserClient)

public:
  IOReturn clientClose() override { terminate(); return kIOReturnSuccess; }
  IOReturn externalMethod(uint32_t selector, IOExternalMethodArguments *args, IOExternalMethodDispatch *, OSObject *,
                          void *) override {
    IOReturn rc = watchdog_dispatch(selector, args);
    aks_note("NativeWatchdog", selector + 64, rc);  // a separate slot range from the key store
    return rc;
  }
};
OSDefineMetaClassAndStructors(IOWatchdogUserClient, IOUserClient)

class IOWatchdog final : public IOService {
  OSDeclareDefaultStructors(IOWatchdog)
  IOReturn open(task_t task, void *security, UInt32 type, OSDictionary *properties, IOUserClient **out) {
    if (!out) return kIOReturnBadArgument;
    *out = nullptr;
    IOWatchdogUserClient *client = OSTypeAlloc(IOWatchdogUserClient);
    if (!client) return kIOReturnNoMemory;
    if (!client->initWithTask(task, security, type, properties) || !client->attach(this)) {
      client->release();
      return kIOReturnError;
    }
    if (!client->start(this)) {
      client->detach(this);
      client->release();
      return kIOReturnError;
    }
    *out = client;
    return kIOReturnSuccess;
  }

public:
  bool start(IOService *provider) override {
    uint32_t enabled = 0;
    if (!PE_parse_boot_argn("sheng-watchdog", &enabled, sizeof(enabled)) || enabled != 1) return false;
    if (!IOService::start(provider)) return false;
    registerService();
    IOLog("NativeWatchdog: started, monitoring reported disabled\n");
    return true;
  }
  IOReturn newUserClient(task_t task, void *security, UInt32 type, OSDictionary *properties, IOUserClient **out) override {
    return open(task, security, type, properties, out);
  }
  IOReturn newUserClient(task_t task, void *security, UInt32 type, IOUserClient **out) override {
    return open(task, security, type, nullptr, out);
  }
};
OSDefineMetaClassAndStructors(IOWatchdog, IOService)
