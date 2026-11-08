// SPDX-License-Identifier: GPL-3.0-or-later
// Single-touch absolute pointer for macOS from the finger set published by the NT36532E bring-up
// (UniXNU-sheng package/TouchProbe.cpp publish()): x along the long panel axis 0..30479, y along the short axis
// 0..20319 (already mirrored by the bring-up), both in 0.1 pixel of the 3048x2032 landscape panel.
// WindowServer does not use multi-touch screens, so the oldest finger drives a pointer and "finger down" is the
// left button. Pure logic; the IOKit glue (native_touch.cpp) owns the HID device. Host-tested.
#pragma once
#include <stdint.h>

namespace sheng_touch {

constexpr int kLongMax = 30479, kShortMax = 20319, kLogicalMax = 32767;
constexpr unsigned kSlots = 12;

// Report descriptor: 3 buttons + 5 padding bits, then 16-bit absolute X and Y in 0..32767 (like a USB tablet).
static const uint8_t kPointerDescriptor[] = {
  0x05, 0x01, 0x09, 0x02, 0xa1, 0x01,             // Generic Desktop, Mouse, Application
  0x09, 0x01, 0xa1, 0x00,                         //   Pointer, Physical
  0x05, 0x09, 0x19, 0x01, 0x29, 0x03,             //     Buttons 1..3
  0x15, 0x00, 0x25, 0x01, 0x95, 0x03, 0x75, 0x01, 0x81, 0x02,
  0x95, 0x01, 0x75, 0x05, 0x81, 0x01,             //     padding
  0x05, 0x01, 0x09, 0x30, 0x09, 0x31,             //     X, Y
  0x15, 0x00, 0x26, 0xff, 0x7f, 0x35, 0x00, 0x46, 0xff, 0x7f,
  0x75, 0x10, 0x95, 0x02, 0x81, 0x02,             //     absolute
  0xc0, 0xc0,
};
constexpr unsigned kReportBytes = 5;

// Orientation fix-ups chosen at boot (native-touch-orient): bit 0 swaps the axes, bit 1 mirrors X, bit 2 mirrors Y.
struct Orientation { bool swap = false, mirrorX = false, mirrorY = false; };
inline Orientation orientation(uint32_t bits) { return Orientation{(bits & 1) != 0, (bits & 2) != 0, (bits & 4) != 0}; }

struct Finger { int id, x, y; };

struct Pointer {
  bool down = false;
  int id = -1;
  uint16_t x = 0, y = 0;   // last reported logical position (kept for the release report)
};

inline uint16_t scale(int value, int maximum)
{
  if (value < 0) value = 0;
  if (value > maximum) value = maximum;
  return static_cast<uint16_t>((static_cast<int64_t>(value) * kLogicalMax + maximum / 2) / maximum);
}

// Choose the finger to follow: keep the current one while it is present, otherwise the lowest tracking id.
inline int choose(const Pointer &p, const Finger *fingers, unsigned count)
{
  int best = -1;
  for (unsigned i = 0; i < count && i < kSlots; ++i) {
    if (fingers[i].x < 0 || fingers[i].y < 0) continue;
    if (fingers[i].id == p.id && p.down) return static_cast<int>(i);
    if (best < 0 || fingers[i].id < fingers[best].id) best = static_cast<int>(i);
  }
  return best;
}

// Update the pointer from one published finger set; returns true when a report should be sent.
inline bool update(Pointer &p, const Finger *fingers, unsigned count, Orientation o, uint8_t report[kReportBytes])
{
  const int index = choose(p, fingers, count);
  if (index < 0) {
    if (!p.down) return false;
    p.down = false;
    p.id = -1;
  } else {
    int lx = fingers[index].x, ly = fingers[index].y, lmax = kLongMax, smax = kShortMax;
    if (o.swap) { int t = lx; lx = ly; ly = t; t = lmax; lmax = smax; smax = t; }
    uint16_t x = scale(lx, lmax), y = scale(ly, smax);
    if (o.mirrorX) x = static_cast<uint16_t>(kLogicalMax - x);
    if (o.mirrorY) y = static_cast<uint16_t>(kLogicalMax - y);
    if (p.down && p.id == fingers[index].id && p.x == x && p.y == y) return false;
    p.down = true;
    p.id = fingers[index].id;
    p.x = x;
    p.y = y;
  }
  report[0] = p.down ? 1 : 0;
  report[1] = static_cast<uint8_t>(p.x);
  report[2] = static_cast<uint8_t>(p.x >> 8);
  report[3] = static_cast<uint8_t>(p.y);
  report[4] = static_cast<uint8_t>(p.y >> 8);
  return true;
}

}  // namespace sheng_touch
