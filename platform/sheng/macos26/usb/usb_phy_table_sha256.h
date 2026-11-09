// SPDX-License-Identifier: GPL-3.0-or-later
// Size and SHA-256 of the local-only PHY table the packager injects into /arm-io/native-usb. The driver refuses any other table.
#pragma once
#include <stddef.h>
#include <stdint.h>
#include "touch_firmware_sha256.h"
namespace sheng_usb {
constexpr size_t kPhyTableBytes = 138;
static const uint8_t kPhyTableSha256[32] = {
    0x0e, 0x78, 0x34, 0xaa, 0xf5, 0xbd, 0x67, 0xac, 0x27, 0x77, 0x04, 0x01, 0x61, 0x10, 0x65, 0x88,
    0x62, 0x9d, 0xfa, 0x8e, 0x74, 0x49, 0x32, 0x8b, 0xe9, 0x91, 0x17, 0x88, 0x86, 0x2f, 0xb5, 0x1f,
};

inline bool phyTableAccepted(const uint8_t *data, size_t size)
{
    uint8_t digest[32];
    if (size != kPhyTableBytes || !sheng::touch_firmware::digest(data, size, digest))
        return false;
    uint8_t different = 0;
    for (unsigned i = 0; i < 32; ++i)
        different |= digest[i] ^ kPhyTableSha256[i];
    return different == 0;
}
}  // namespace sheng_usb
