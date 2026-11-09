/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef SHENG_WCN_STATE_H
#define SHENG_WCN_STATE_H

/*
 * Read-only state of the WCN7850 power inputs, and the report rows of the Wi-Fi stage 1 snapshot.
 * Pure logic over the SPMI observer of spmi_keys.h and plain register values, so the host tests can drive it; no IOKit here.
 *
 * Sources (offline; evidence level "source assertion" until a boot prints the values):
 *  - Linux sm8550-xiaomi-sheng.dts wcn7850-pmu: vdd vreg_s5g_0p8, vddio vreg_l15b_1p8, vddaon vreg_s2g_0p8, vdddig vreg_s4e_0p9,
 *    vddrfa1p2 vreg_s4g_1p3, vddrfa1p8 vreg_s6g_1p8; pinctrl pmk8550_sleep_clk (PMK8550 gpio3, func1, output).
 *  - UniXNU-sheng package/WifiProbe.cpp at a68ee4e (BSD-2-Clause-Patent): rail peripherals s5g 0x6a7, s2g 0x69e, s4e 0x4a4, s4g 0x6a4,
 *    s6g 0x6aa, by its own PMIC readings; regulator registers type 0x04, subtype 0x05, status 0x08 (bit 7 in regulation), enable 0x46
 *    (bit 7), setpoint in millivolts 0x40/0x41; PMK8550 gpio3 is peripheral 0x0ba of SID 0.
 *  - Hypothesis, not in upstream: vddio LDO15 of PM8550 (SID 1) at page 0x4e. The row prints type and subtype, so a wrong address shows.
 *
 * Only reads are issued, through the observer channels. Nothing here selects a write channel or writes a PMIC register.
 */

#include <stddef.h>
#include <stdint.h>
#if __has_include(<libkern/libkern.h>)
#include <libkern/libkern.h>
#else
#include <stdio.h>
#endif
#include "spmi_keys.h"

namespace sheng_wcn {

using sheng_keys::Arbiter;
using sheng_keys::Status;

constexpr uint8_t kRegType = 0x04, kRegSubtype = 0x05, kRegStatus = 0x08, kRegVoltageLow = 0x40, kRegVoltageHigh = 0x41, kRegEnable = 0x46;
constexpr uint8_t kTypeBuck = 0x03, kTypeLdo = 0x04, kTypeFts = 0x1c, kTypeUlt1 = 0x21, kTypeUlt2 = 0x22;
constexpr uint8_t kRegulatorInRegulation = 0x80, kRegulatorEnabled = 0x80;

struct RailSpec {
    const char *name;
    uint16_t sid;
    uint8_t page;
    uint16_t nominalMillivolts;
};

constexpr RailSpec kRails[] = {
    {"vdd-s5g", 6, 0xa7, 850},  {"vddio-l15b", 1, 0x4e, 1800}, {"vddaon-s2g", 6, 0x9e, 850},
    {"vdig-s4e", 4, 0xa4, 950}, {"rf12-s4g", 6, 0xa4, 1300},   {"rf18-s6g", 6, 0xaa, 1860},
};
constexpr unsigned kRailCount = sizeof kRails / sizeof kRails[0];

struct RailState {
    int apid = -1;
    Status status = Status::notFound;
    uint8_t type = 0, subtype = 0, regulation = 0, enable = 0;
    uint16_t millivolts = 0;
    bool knownType() const
    {
        return type == kTypeBuck || type == kTypeLdo || type == kTypeFts || type == kTypeUlt1 || type == kTypeUlt2;
    }
    bool on() const { return status == Status::ok && knownType() && (enable & kRegulatorEnabled); }
};

inline Status readRail(Arbiter &a, const RailSpec &spec, RailState *out)
{
    *out = RailState();
    out->apid = sheng_keys::findApid(a, spec.sid, spec.page);
    if (out->apid < 0)
        return out->status = Status::notFound;
    uint8_t low = 0, high = 0;
    Status s = sheng_keys::readByte(a, out->apid, kRegType, &out->type);
    if (s == Status::ok)
        s = sheng_keys::readByte(a, out->apid, kRegSubtype, &out->subtype);
    if (s == Status::ok)
        s = sheng_keys::readByte(a, out->apid, kRegStatus, &out->regulation);
    if (s == Status::ok)
        s = sheng_keys::readByte(a, out->apid, kRegEnable, &out->enable);
    if (s == Status::ok)
        s = sheng_keys::readByte(a, out->apid, kRegVoltageLow, &low);
    if (s == Status::ok)
        s = sheng_keys::readByte(a, out->apid, kRegVoltageHigh, &high);
    if (s == Status::ok)
        out->millivolts = static_cast<uint16_t>(high << 8 | low);
    return out->status = s;
}

constexpr uint16_t kSleepClockSid = 0;
constexpr uint8_t kSleepClockPage = 0xba;
constexpr uint8_t kPinMode = 0x40, kPinVoltage = 0x41, kPinPull = 0x42, kPinSource = 0x44, kPinEnable = 0x46;
constexpr uint8_t kPinSubtypeLow = 0x10, kPinSubtypeHigh = 0x13;

struct SleepClockState {
    int apid = -1, owner = -1;
    Status status = Status::notFound;
    uint8_t type = 0, subtype = 0, mode = 0, voltage = 0, pull = 0, source = 0, enable = 0;
    // What the WCN7850 PMU needs from PMK8550 gpio3: master enable, output mode, source function 2 (func1), and the pin layout this reads.
    bool driving() const
    {
        return status == Status::ok && subtype >= kPinSubtypeLow && subtype <= kPinSubtypeHigh && (enable & 0x80) && (mode & 3) == 1 &&
               (source & 0xf) == 2;
    }
};

inline Status readSleepClock(Arbiter &a, SleepClockState *out)
{
    *out = SleepClockState();
    out->apid = sheng_keys::findApid(a, kSleepClockSid, kSleepClockPage);
    if (out->apid < 0)
        return out->status = Status::notFound;
    if (4 * static_cast<uint32_t>(out->apid) + 4 <= a.cnfgBytes)
        out->owner = static_cast<int>(a.io->cnfg(4 * static_cast<uint32_t>(out->apid)) & 7);
    const struct { uint8_t reg; uint8_t *to; } reads[] = {
        {kRegType, &out->type},  {kRegSubtype, &out->subtype}, {kPinMode, &out->mode},     {kPinVoltage, &out->voltage},
        {kPinPull, &out->pull},  {kPinSource, &out->source},   {kPinEnable, &out->enable},
    };
    for (const auto &r : reads) {
        const Status s = sheng_keys::readByte(a, out->apid, r.reg, r.to);
        if (s != Status::ok)
            return out->status = s;
    }
    return out->status = Status::ok;
}

inline const char *statusName(Status s)
{
    switch (s) {
    case Status::ok: return "ok";
    case Status::unsupported: return "unsupported";
    case Status::notFound: return "absent";
    case Status::timeout: return "timeout";
    case Status::failure: return "failure";
    case Status::denied: return "denied";
    case Status::dropped: return "dropped";
    }
    return "?";
}

// Report rows. Every row fits the 88 columns of the boot log after its "NativeWifi t120 Xn " prefix (19 columns), so a photo of the
// screen can be read; kRowBytes also bounds the kernel buffers.
constexpr size_t kRowBytes = 72;

inline void formatRail(char *out, size_t bytes, const RailSpec &spec, const RailState &s)
{
    if (s.status != Status::ok)
        snprintf(out, bytes, "%-10s ppid %03x %s", spec.name, (unsigned)(spec.sid << 8 | spec.page), statusName(s.status));
    else
        snprintf(out, bytes, "%-10s a%03d t%02x/%02x st%02x en%02x %4umV want %4u", spec.name, s.apid, s.type, s.subtype, s.regulation,
                 s.enable, s.millivolts, spec.nominalMillivolts);
}

inline void formatSleepClock(char *out, size_t bytes, const SleepClockState &s)
{
    if (s.status != Status::ok)
        snprintf(out, bytes, "sleepclk %s", statusName(s.status));
    else
        snprintf(out, bytes, "sleepclk a%03d own%d t%02x/%02x md%02x v%02x pu%02x src%02x en%02x %s", s.apid, s.owner, s.type, s.subtype, s.mode,
                 s.voltage, s.pull, s.source, s.enable, s.driving() ? "driving" : "idle");
}

// The ten words of native_wifi_stats, as upstream's stage 1 fills them.
constexpr unsigned kWifiStatsWords = 10;

inline void formatClocks(char *out, size_t bytes, const uint32_t *st)
{
    snprintf(out, bytes, "st%d v8 %08x v0 %08x gdsc %08x off %03x", static_cast<int>(st[0]), st[1], st[2], st[3], st[4]);
}

inline void formatLink(char *out, size_t bytes, const uint32_t *st)
{
    snprintf(out, bytes, "pin %06x ltssm %08x rc %08x lnk %08x phy %08x", st[5], st[6], st[7], st[8], st[9]);
}

// TLMM pin state: configuration word and the output/input bits of the IO word (bit 0 input level, bit 1 output level).
constexpr unsigned kPinCount = 10, kPinsPerRow = 5;
constexpr uint8_t kPins[kPinCount] = {76, 77, 78, 79, 81, 80, 82, 94, 95, 96};  // UART14 of Bluetooth, BT_EN, WLAN_EN, SW_CTRL, PERST#, CLKREQ#, WAKE#

struct PinState {
    uint32_t config = 0, io = 0;
};

inline void formatPins(char *out, size_t bytes, unsigned first, const PinState *pins)
{
    size_t used = 0;
    for (unsigned i = first; i < first + kPinsPerRow && i < kPinCount && used < bytes; ++i) {
        const int n = snprintf(out + used, bytes - used, "%s%u:%04x/%x", i == first ? "" : " ", kPins[i], pins[i].config & 0xffff, pins[i].io & 3);
        if (n < 0)
            return;
        used += static_cast<size_t>(n);
    }
}

}  // namespace sheng_wcn

#endif
