// SPDX-License-Identifier: GPL-3.0-or-later
// Host test of the single-touch absolute pointer mapping (platform/sheng/macos26/input/touch_pointer.h).
#include <cassert>
#include <cstdint>
#include "../platform/sheng/macos26/input/touch_pointer.h"

using namespace sheng_touch;

static uint16_t x16(const uint8_t *r) { return static_cast<uint16_t>(r[1] | r[2] << 8); }
static uint16_t y16(const uint8_t *r) { return static_cast<uint16_t>(r[3] | r[4] << 8); }

int main()
{
  static_assert(sizeof(kPointerDescriptor) == 56, "pointer descriptor length");
  uint8_t r[kReportBytes];
  {  // Corners and centre of the 3048x2032 landscape panel map onto 0..32767.
    Pointer p;
    Finger f[1] = {{7, 0, 0}};
    assert(update(p, f, 1, Orientation(), r) && r[0] == 1 && x16(r) == 0 && y16(r) == 0);
    f[0] = {7, kLongMax, kShortMax};
    assert(update(p, f, 1, Orientation(), r) && x16(r) == kLogicalMax && y16(r) == kLogicalMax);
    f[0] = {7, 15240, 10160};
    assert(update(p, f, 1, Orientation(), r) && x16(r) > 16380 && x16(r) < 16390 && y16(r) > 16380 && y16(r) < 16390);
    assert(!update(p, f, 1, Orientation(), r));   // unchanged position: no report
    assert(update(p, f, 0, Orientation(), r) && r[0] == 0 && x16(r) > 16380);   // release keeps the last position
    assert(!update(p, f, 0, Orientation(), r));
  }
  {  // The first finger keeps the pointer while a second finger lands and leaves.
    Pointer p;
    Finger a[1] = {{5, 1000, 1000}};
    assert(update(p, a, 1, Orientation(), r));
    Finger two[2] = {{3, 20000, 15000}, {5, 1100, 1000}};
    assert(update(p, two, 2, Orientation(), r) && p.id == 5);
    Finger b[1] = {{3, 20000, 15000}};
    assert(update(p, b, 1, Orientation(), r) && p.id == 3 && r[0] == 1);
  }
  {  // Out-of-range values are clamped; negative coordinates (no position) are ignored.
    Pointer p;
    Finger f[2] = {{1, -1, -1}, {2, 40000, 30000}};
    assert(update(p, f, 2, Orientation(), r) && p.id == 2 && x16(r) == kLogicalMax && y16(r) == kLogicalMax);
  }
  {  // Orientation bits: swap, mirror X, mirror Y.
    Pointer p;
    Finger f[1] = {{1, kLongMax, 0}};
    assert(update(p, f, 1, orientation(2), r) && x16(r) == 0 && y16(r) == 0);
    Pointer q;
    assert(update(q, f, 1, orientation(4), r) && x16(r) == kLogicalMax && y16(r) == kLogicalMax);
    Pointer s;
    assert(update(s, f, 1, orientation(1), r) && x16(r) == 0 && y16(r) == kLogicalMax);
  }
  return 0;
}
