// SPDX-License-Identifier: GPL-3.0-or-later
// The container that carries the two ADSP images in the loader's addon slot (kernel.macho.gpuaddon, handed over as
// /chosen/memory-map/GpuAddon). The loader itself only checks the first magic and the total size at offset 16. scripts/native_adsp.py
// writes this layout; the images never enter a build, only a local package.
//
//   0   "NGPUADD2"       the loader's magic
//   8   "SHADSP01"       this container (anything else in the slot is not ours)
//   16  u64 total bytes  the loader checks this equals the file size
//   24  u64 adsp offset, 32 u64 adsp bytes, 40 adsp SHA-256 (32 bytes)
//   72  u64 dtb offset,  80 u64 dtb bytes,  88 dtb SHA-256 (32 bytes)
//   120 u64 reserved (zero); images follow, each aligned to 4 KiB
#pragma once
#include <stddef.h>
#include <stdint.h>

namespace sheng_adsp {

constexpr uint64_t kContainerHeaderBytes = 128;

enum {ContainerOk = 0, ContainerTooSmall = -101, ContainerMagic = -102, ContainerKind = -103, ContainerSize = -104, ContainerRange = -105};

struct ContainerImage {
  const uint8_t *bytes;
  uint64_t size;
  const uint8_t *sha256;  // 32 bytes
};

inline uint64_t containerU64(const uint8_t *p)
{
  uint64_t value = 0;
  for (unsigned i = 0; i < 8; ++i) value |= uint64_t(p[i]) << (8 * i);
  return value;
}

inline bool containerTag(const uint8_t *p, const char *tag)
{
  for (unsigned i = 0; i < 8; ++i)
    if (p[i] != (uint8_t)tag[i]) return false;
  return true;
}

inline bool containerRange(uint64_t offset, uint64_t bytes, uint64_t total)
{
  return bytes && offset >= kContainerHeaderBytes && offset <= total && bytes <= total - offset;
}

// Finds the two images; nothing is hashed here. Every offset comes from a file, so each is bounds checked.
inline int parseContainer(const uint8_t *blob, uint64_t size, ContainerImage &adsp, ContainerImage &dtb)
{
  adsp = {nullptr, 0, nullptr};
  dtb = {nullptr, 0, nullptr};
  if (!blob || size < kContainerHeaderBytes) return ContainerTooSmall;
  if (!containerTag(blob, "NGPUADD2")) return ContainerMagic;
  if (!containerTag(blob + 8, "SHADSP01")) return ContainerKind;
  if (containerU64(blob + 16) != size) return ContainerSize;
  const uint64_t adspAt = containerU64(blob + 24), adspBytes = containerU64(blob + 32);
  const uint64_t dtbAt = containerU64(blob + 72), dtbBytes = containerU64(blob + 80);
  if (!containerRange(adspAt, adspBytes, size) || !containerRange(dtbAt, dtbBytes, size)) return ContainerRange;
  adsp = {blob + adspAt, adspBytes, blob + 40};
  dtb = {blob + dtbAt, dtbBytes, blob + 88};
  return ContainerOk;
}

}  // namespace sheng_adsp
