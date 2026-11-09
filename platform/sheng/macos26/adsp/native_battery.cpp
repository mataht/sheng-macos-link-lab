// SPDX-License-Identifier: BSD-2-Clause-Patent
// NativeBattery: the battery as macOS reads it, an IOPMPowerSource whose values come from the battery manager in the audio DSP over the
// PMIC GLINK channel (upstream ShengBattmgr.c). Derived from UniXNU-sheng package/ShengBattery.cpp at
// a68ee4ee67cc71792fbf519de7e3e28072c717b5, which is proven on iPadOS only.
//
// Started by NativeAdsp under sheng-adsp=3 and sheng-battery:
//   1  reads the battery manager and prints the values; nothing is published, powerd sees no battery.
//   2  also publishes the IOPMPowerSource (registerService), for pmset -g batt and the menu bar.
// Changes from upstream: no weak imports (USB-C and VBUS consumers register at run time, below); the USB input current limit is not
// written unless sheng-usb-sdp-ma asks for it (upstream defaults to 900 mA), so a default run writes nothing to the charger; the critical
// level is never asserted until this has been qualified, because a low-battery shutdown is unproven here and upstream records that it
// panics in vnode_rele_ext during unmount; a kext has real C++ startup, so upstream's BatteryProbe.cpp is not needed.
//
// Design (upstream's): everything runs on the service's work loop. The DSP thread's callbacks only copy a message into a small ring and
// signal an IOInterruptEventSource (no provider: a software event), so the DSP thread never waits for this driver. One request is in
// flight at a time; a 2 s timer gives up on an unanswered one. Battery notifications start a refresh at once, and a 60 s timer refreshes
// in any case (the fields that hardly change, every tenth time). Charging itself is the DSP firmware's.
#include <IOKit/IOLib.h>
#include <IOKit/IOWorkLoop.h>
#include <IOKit/IOTimerEventSource.h>
#include <IOKit/IOInterruptEventSource.h>
#include <IOKit/IOPlatformExpert.h>
#include <IOKit/pwr_mgt/IOPM.h>
#include <IOKit/pwr_mgt/IOPMPowerSource.h>
#include <libkern/c++/OSBoolean.h>
#include <libkern/c++/OSDictionary.h>
#include <libkern/c++/OSNumber.h>
#include <libkern/c++/OSSymbol.h>
#include <pexpert/pexpert.h>
#include <string.h>
extern "C" {
#include "ShengAdsp.h"
#include "ShengBattmgr.h"
}

// sizeof(IOPMPowerSource) in the 25F71 kernel's DWARF; the class layout is compiled in, so a different header must fail the build.
static_assert(sizeof(IOPMPowerSource) == 0x138, "paired 25F71 IOPMPowerSource size");

// [0] stage (1 class up, 2 channel registered, 3 channel up, 4 published or would be; negative: the step that failed), [1] percent, [2] mV,
// [3] mA, [4] temperature (raw), [5] flags (bit 0 present, 1 external, 2 charging, 3 full, 4 notifications enabled), [6] refresh
// cycles, [7] answers, [8] timeouts | errors << 16, [9] notifications, [10] USB type (raw), [11] full mAh, [12] design mAh,
// [13] cycles, [14] messages dropped (ring full) | ignored << 16, [15] send refusals
extern "C" {volatile int32_t native_battery_stats[16];}

// USB-C and VBUS consumers (a USB driver in the same kext) register here; none registered means no effect.
typedef struct {
  uint32_t (*start)(uint8_t *out);
  uint32_t (*receive)(const uint8_t *data, uint32_t length, uint8_t *out);
  uint32_t (*tick)(uint8_t *out);
  bool (*polling)(void);
} NativeBatteryUcsi;
static const NativeBatteryUcsi *gUcsi;
static void (*gVbus)(int);
extern "C" void native_battery_register_ucsi(const NativeBatteryUcsi *ops) {__atomic_store_n(&gUcsi, ops, __ATOMIC_RELEASE);}
extern "C" void native_battery_register_vbus(void (*notify)(int)) {__atomic_store_n(&gVbus, notify, __ATOMIC_RELEASE);}

namespace {
constexpr uint32_t kRequestMs = 2000, kRefreshMs = 60000, kSlowEvery = 10, kRing = 16, kMessage = 560, kUcsiOwner = 32779;
}

class NativeBattery : public IOPMPowerSource {
  OSDeclareDefaultStructors(NativeBattery)

  IOWorkLoop *loop;
  IOTimerEventSource *request, *refresh, *ucsiTimer;
  IOInterruptEventSource *inbox;
  IOSimpleLock *ringLock;
  struct Slot {uint32_t length; uint8_t data[kMessage];} ring[kRing];
  uint32_t ringHead, ringCount;
  volatile uint32_t channelUp, channelChanged;
  bool wasUp, published;
  uint32_t refreshes, sdpLimit, stageWanted;
  ShengBattmgr bm;
  ShengAdspChannel *channel;
  uint8_t ucsiOut[kMessage];

  void ucsiSend(uint32_t n)
  {
    if (n) sheng_adsp_send(channel, ucsiOut, n);
    const NativeBatteryUcsi *ops = __atomic_load_n(&gUcsi, __ATOMIC_ACQUIRE);
    if (ops && ops->polling && ops->polling()) ucsiTimer->setTimeoutMS(20);
  }
  static void ucsiTick(OSObject *owner, IOTimerEventSource *)
  {
    NativeBattery *self = OSDynamicCast(NativeBattery, owner);
    const NativeBatteryUcsi *ops = __atomic_load_n(&gUcsi, __ATOMIC_ACQUIRE);
    if (self && self->wasUp && ops && ops->tick) self->ucsiSend(ops->tick(self->ucsiOut));
  }

  static void dspState(void *context, int up)
  {
    NativeBattery *self = static_cast<NativeBattery *>(context);
    __atomic_store_n(&self->channelUp, up ? 1u : 0u, __ATOMIC_RELEASE);
    __atomic_store_n(&self->channelChanged, 1u, __ATOMIC_RELEASE);
    self->inbox->interruptOccurred(nullptr, nullptr, 0);
  }
  static void dspReceive(void *context, const uint8_t *data, uint32_t length)
  {
    NativeBattery *self = static_cast<NativeBattery *>(context);
    IOSimpleLockLock(self->ringLock);
    if (self->ringCount < kRing && length <= kMessage) {
      Slot &slot = self->ring[(self->ringHead + self->ringCount) % kRing];
      memcpy(slot.data, data, length);
      slot.length = length;
      self->ringCount++;
    } else {
      native_battery_stats[14] = native_battery_stats[14] + 1;
    }
    IOSimpleLockUnlock(self->ringLock);
    self->inbox->interruptOccurred(nullptr, nullptr, 0);
  }

  void pump()
  {
    uint8_t out[SHENG_BATTMGR_REQUEST_BYTES];
    const uint32_t n = sheng_battmgr_next(&bm, out);
    if (!n) return;
    if (sheng_adsp_send(channel, out, n)) native_battery_stats[15] = native_battery_stats[15] + 1;  // the timer skips the field
    request->setTimeoutMS(kRequestMs);
  }
  void number(const char *key, int64_t value)
  {
    const OSSymbol *symbol = OSSymbol::withCString(key);
    OSNumber *n = OSNumber::withNumber((unsigned long long)value, 32);
    if (symbol && n) setPSProperty(symbol, n);
    OSSafeReleaseNULL(n);
    OSSafeReleaseNULL(symbol);
  }
  void flag(const char *key, bool value)
  {
    const OSSymbol *symbol = OSSymbol::withCString(key);
    if (symbol) setPSProperty(symbol, value ? kOSBooleanTrue : kOSBooleanFalse);
    OSSafeReleaseNULL(symbol);
  }
  void setAdapter(const ShengBatterySnapshot &s)
  {
    OSDictionary *adapter = OSDictionary::withCapacity(5);
    if (!adapter) return;
    OSNumber *family = OSNumber::withNumber(s.adapter_family, 32);
    if (family) {adapter->setObject(kIOPMPSAdapterDetailsFamilyKey, family); family->release();}
    if (s.external) {
      OSNumber *w = OSNumber::withNumber((unsigned long long)s.adapter_watts, 32), *mv = OSNumber::withNumber((unsigned long long)s.adapter_mv, 32),
               *ma = OSNumber::withNumber((unsigned long long)s.adapter_ma, 32);
      if (w) {adapter->setObject(kIOPMPSAdapterDetailsWattsKey, w); w->release();}
      if (mv) {adapter->setObject(kIOPMPSAdapterDetailsVoltage, mv); mv->release();}
      if (ma) {adapter->setObject(kIOPMPSAdapterDetailsAmperageKey, ma); ma->release();}
    }
    const OSSymbol *key = OSSymbol::withCString(kIOPMPSAdapterDetailsKey);
    if (key) setPSProperty(key, adapter);
    OSSafeReleaseNULL(key);
    adapter->release();
  }
  void publish()
  {
    ShengBatterySnapshot s;
    sheng_battmgr_snapshot(&bm, &s);
    setBatteryInstalled(s.present);
    setExternalConnected(s.external);
    setExternalChargeCapable(s.external);
    setIsCharging(s.charging);
    setAtCriticalLevel(false);  // see the head comment: not asserted until a low-battery shutdown is qualified
    setCurrentCapacity((unsigned)s.percent);
    setMaxCapacity(100);
    setVoltage((unsigned)(s.voltage_mv > 0 ? s.voltage_mv : 0));
    setAmperage(s.current_ma);
    if (s.cycles > 0) setCycleCount((unsigned)s.cycles);
    setTimeRemaining(s.charging ? (s.time_to_full_min >= 0 ? s.time_to_full_min : 0xffff)
                                : (s.time_to_empty_min >= 0 ? s.time_to_empty_min : 0xffff));
    flag("FullyCharged", s.fully_charged);
    number("InstantAmperage", s.current_ma);
    if (s.temperature_valid) number("Temperature", s.temperature);
    if (s.raw_mah > 0) number("AppleRawCurrentCapacity", s.raw_mah);
    if (s.full_mah > 0) {number("AppleRawMaxCapacity", s.full_mah); number("NominalChargeCapacity", s.full_mah);}
    if (s.design_mah > 0) number("DesignCapacity", s.design_mah);
    setAdapter(s);
    native_battery_stats[1] = s.percent; native_battery_stats[2] = s.voltage_mv; native_battery_stats[3] = s.current_ma;
    native_battery_stats[4] = s.temperature;
    native_battery_stats[5] = s.present | s.external << 1 | s.charging << 2 | s.fully_charged << 3 | bm.enabled << 4;
    native_battery_stats[10] = (bm.valid >> BmUsbType) & 1 ? bm.value[BmUsbType] : -1;
    native_battery_stats[11] = s.full_mah; native_battery_stats[12] = s.design_mah; native_battery_stats[13] = s.cycles;
    void (*vbus)(int) = __atomic_load_n(&gVbus, __ATOMIC_ACQUIRE);
    if (vbus && (bm.valid >> BmUsbOnline) & 1) vbus(bm.value[BmUsbOnline]);
    IOLog("NativeBattery: %d%% %d mV %d mA temp %d present %d external %d charging %d full %d; raw %d/%d mAh design %d cycles %d; usb type %d; "
          "cycles %u answers %u timeouts %u errors %u valid %x\n",
          s.percent, s.voltage_mv, s.current_ma, s.temperature, s.present, s.external, s.charging, s.fully_charged, s.raw_mah, s.full_mah, s.design_mah,
          s.cycles, native_battery_stats[10], bm.cycles_done, bm.responses, bm.timeouts, bm.errors, bm.valid);
    if (!published && !s.charge_known) return;  // no charge reading yet: 0 % would read as an empty battery
    if (!published) {
      // The properties are in place before anyone finds the service.
      published = true;
      native_battery_stats[0] = 4;
      if (stageWanted >= 2) {
        registerService();
        IOLog("NativeBattery: published as an IOPMPowerSource\n");
      }
    }
    if (stageWanted >= 2) updateStatus();
  }
  void counters()
  {
    native_battery_stats[6] = (int32_t)bm.cycles_done;
    native_battery_stats[7] = (int32_t)bm.responses;
    native_battery_stats[8] = (int32_t)(bm.timeouts | bm.errors << 16);
    native_battery_stats[9] = (int32_t)bm.notifications;
    native_battery_stats[14] = (native_battery_stats[14] & 0xffff) | (int32_t)(bm.ignored << 16);
  }
  void handle(int result)
  {
    if (result == BmCycleDone) publish();
    pump();
    counters();
  }

  static void inboxAction(OSObject *owner, IOInterruptEventSource *, int)
  {
    NativeBattery *self = OSDynamicCast(NativeBattery, owner);
    if (!self) return;
    if (__atomic_exchange_n(&self->channelChanged, 0u, __ATOMIC_ACQ_REL)) {
      const bool up = __atomic_load_n(&self->channelUp, __ATOMIC_ACQUIRE) != 0;
      if (up && !self->wasUp) {
        native_battery_stats[0] = self->published ? 4 : 3;
        IOLog("NativeBattery: channel up\n");
        uint8_t enable[SHENG_BATTMGR_ENABLE_BYTES];
        sheng_adsp_send(self->channel, enable, sheng_battmgr_enable_request(enable));
        const NativeBatteryUcsi *ops = __atomic_load_n(&gUcsi, __ATOMIC_ACQUIRE);
        if (ops && ops->start) self->ucsiSend(ops->start(self->ucsiOut));
        sheng_battmgr_refresh(&self->bm, 1);
        self->pump();
        self->refresh->setTimeoutMS(kRefreshMs);
      }
      if (!up && self->wasUp) {
        IOLog("NativeBattery: channel down\n");
        self->request->cancelTimeout();
        sheng_battmgr_init(&self->bm);
        self->bm.sdp_limit_ua = self->sdpLimit;
      }
      self->wasUp = up;
    }
    for (;;) {
      uint8_t data[kMessage];
      uint32_t length = 0;
      IOSimpleLockLock(self->ringLock);
      if (self->ringCount) {
        Slot &slot = self->ring[self->ringHead];
        length = slot.length;
        memcpy(data, slot.data, length);
        self->ringHead = (self->ringHead + 1) % kRing;
        self->ringCount--;
      }
      IOSimpleLockUnlock(self->ringLock);
      if (!length) break;
      const NativeBatteryUcsi *ops = __atomic_load_n(&gUcsi, __ATOMIC_ACQUIRE);
      if (ops && ops->receive && length >= 4 && (data[0] | data[1] << 8 | data[2] << 16 | (uint32_t)data[3] << 24) == kUcsiOwner) {
        self->ucsiSend(ops->receive(data, length, self->ucsiOut));
        continue;
      }
      const int result = sheng_battmgr_receive(&self->bm, data, length);
      if (result == BmAnswer || result == BmCycleDone) self->request->cancelTimeout();
      self->handle(result);
    }
  }
  static void requestAction(OSObject *owner, IOTimerEventSource *)
  {
    NativeBattery *self = OSDynamicCast(NativeBattery, owner);
    if (!self || !self->wasUp) return;
    const uint32_t before = self->bm.cycles_done;
    sheng_battmgr_timeout(&self->bm);
    self->handle(self->bm.cycles_done != before ? BmCycleDone : BmAnswer);
  }
  static void refreshAction(OSObject *owner, IOTimerEventSource *sender)
  {
    NativeBattery *self = OSDynamicCast(NativeBattery, owner);
    if (!self || !self->wasUp) return;
    sheng_battmgr_refresh(&self->bm, ++self->refreshes % kSlowEvery == 0);
    self->handle(BmAnswer);
    sender->setTimeoutMS(kRefreshMs);
  }

public:
  bool startBattery(unsigned stage)
  {
    stageWanted = stage;
    sheng_battmgr_init(&bm);
    uint32_t ma = 0;
    bm.sdp_limit_ua = 0;  // no write to the charger unless asked for
    if (PE_parse_boot_argn("sheng-usb-sdp-ma", &ma, sizeof ma)) bm.sdp_limit_ua = ma > 3000 ? 3000000u : ma * 1000u;
    sdpLimit = bm.sdp_limit_ua;
    loop = IOWorkLoop::workLoop();
    ringLock = IOSimpleLockAlloc();
    inbox = IOInterruptEventSource::interruptEventSource(this, &NativeBattery::inboxAction);
    request = IOTimerEventSource::timerEventSource(this, &NativeBattery::requestAction);
    refresh = IOTimerEventSource::timerEventSource(this, &NativeBattery::refreshAction);
    ucsiTimer = IOTimerEventSource::timerEventSource(this, &NativeBattery::ucsiTick);
    if (!loop || !ringLock || !inbox || !request || !refresh || !ucsiTimer || loop->addEventSource(inbox) != kIOReturnSuccess ||
        loop->addEventSource(request) != kIOReturnSuccess || loop->addEventSource(refresh) != kIOReturnSuccess ||
        loop->addEventSource(ucsiTimer) != kIOReturnSuccess)
      return false;
    native_battery_stats[0] = 1;
    const ShengAdspClient client = {&NativeBattery::dspState, &NativeBattery::dspReceive};
    channel = sheng_adsp_channel("PMIC_RTR_ADSP_APPS", 1024, 4, 0, &client, this);
    if (!channel) return false;
    native_battery_stats[0] = 2;
    return true;
  }
};

OSDefineMetaClassAndStructors(NativeBattery, IOPMPowerSource)

extern "C" bool native_battery_start(unsigned stage)
{
  NativeBattery *battery = OSTypeAlloc(NativeBattery);
  if (!battery || !battery->init() || !battery->attach(IOService::getPlatform())) {
    if (battery) battery->release();
    native_battery_stats[0] = -1;
    return false;
  }
  if (!battery->startBattery(stage)) {
    native_battery_stats[0] = native_battery_stats[0] > 0 ? -native_battery_stats[0] - 1 : -1;
    battery->detach(IOService::getPlatform());
    battery->release();
    return false;
  }
  battery->release();  // the platform's attachment keeps it
  IOLog("NativeBattery: stage %u started\n", stage);
  return true;
}
