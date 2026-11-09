/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef SHENG_NATIVE_RTC_H
#define SHENG_NATIVE_RTC_H

/*
 * Wall clock of the sheng tablet from the PMK8550 RTC, read through the SPMI arbiter's observer (read) channel.
 * Pure logic over the Mmio accessor of spmi_keys.h so the host test can drive a simulated arbiter; no IOKit here.
 *
 * Sources:
 *  - sheng device tree: qcom,pmk8550@0 (SID 0) rtc@6100, compatible "qcom,pmk8350-rtc" (offline check of the dtbo).
 *  - Linux drivers/rtc/rtc-pm8xxx.c, pmk8350 registers: control 0x6146 (bit 7 = enabled), read data 0x6148..0x614b,
 *    little-endian seconds, carry handled by reading again.
 *  - Same driver: without the device tree property allow-set-time it never writes the counter, it keeps wall clock minus
 *    counter as an offset elsewhere and adds it on read. The sheng rtc@6100 node has no allow-set-time. So a raw count is NOT
 *    a date. It is used only together with a known offset (sheng-rtc-offset, measured from Android as wall clock minus
 *    /sys/class/rtc/rtc0/since_epoch); without one the clock falls back to the packaging time (sheng-rtc-floor).
 * Only reads are issued; no arbiter write channel, PMIC register write or alarm is used.
 */

#include "spmi_keys.h"

namespace sheng_rtc {

constexpr uint16_t kSid = 0, kPeripheral = 0x61, kControl = 0x6146, kData = 0x6148;
constexpr uint8_t kEnabled = 0x80;
constexpr unsigned kReadAttempts = 4;

struct Reading {
    sheng_keys::Status status = sheng_keys::Status::notFound;
    bool enabled = false;
    uint32_t seconds = 0;
    unsigned attempts = 0;
};

inline int channel(sheng_keys::Arbiter &a) { return sheng_keys::findApid(a, kSid, kPeripheral); }

// The four data bytes are read one at a time and the counter may tick between them, so a value counts only when two
// consecutive complete reads agree (one read takes microseconds, the counter ticks once per second).
inline Reading read(sheng_keys::Arbiter &a, int apid)
{
    using sheng_keys::Status;
    Reading r;
    uint8_t control = 0;
    r.status = sheng_keys::readByte(a, apid, kControl, &control);
    if (r.status != Status::ok)
        return r;
    r.enabled = (control & kEnabled) != 0;
    uint32_t previous = 0;
    for (r.attempts = 1; r.attempts <= kReadAttempts; ++r.attempts) {
        uint32_t value = 0;
        for (unsigned i = 0; i < 4; ++i) {
            uint8_t byte = 0;
            r.status = sheng_keys::readByte(a, apid, kData + i, &byte);
            if (r.status != Status::ok)
                return r;
            value |= static_cast<uint32_t>(byte) << (8 * i);
        }
        if (r.attempts > 1 && value == previous) {
            r.seconds = value;
            return r;
        }
        previous = value;
    }
    r.status = Status::dropped;   // never stable: treat as unreadable
    return r;
}

enum class Source : int { none = 0, rtcPlusOffset = 1, floor = 2 };

struct Policy {
    bool haveOffset = false;
    int32_t offset = 0;   // wall clock minus the raw counter, measured from Android
    int32_t floor = 0;    // never start earlier than this (the packaging time); 0 = none
};

struct Time {
    int64_t seconds = 0;  // Unix seconds, 0 when no source is available
    Source source = Source::none;
};

inline Time wallClock(const Reading &r, const Policy &p)
{
    Time t;
    if (r.status == sheng_keys::Status::ok && r.enabled && p.haveOffset) {
        t.seconds = static_cast<int64_t>(r.seconds) + p.offset;
        t.source = Source::rtcPlusOffset;
    }
    if (p.floor > 0 && t.seconds < p.floor) {
        t.seconds = p.floor;
        t.source = Source::floor;
    }
    if (t.seconds <= 0) {
        t.seconds = 0;
        t.source = Source::none;
    }
    return t;
}

inline const char *sourceName(Source s)
{
    switch (s) {
    case Source::rtcPlusOffset: return "RTC + offset";
    case Source::floor: return "packaging time";
    case Source::none: return "none";
    }
    return "?";
}

}  // namespace sheng_rtc

#endif
