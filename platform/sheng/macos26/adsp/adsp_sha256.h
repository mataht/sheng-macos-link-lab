// SPDX-License-Identifier: GPL-3.0-or-later
// Portable SHA-256 for the local-only ADSP images carried in /arm-io/native-adsp (no unpublished kernel imports). It proves the images
// reached the kernel intact and identifies them in the log; the integrity anchor of the code itself is the secure world's signature check.
#pragma once
#include <stddef.h>
#include <stdint.h>

namespace sheng_adsp {

inline uint32_t ror(uint32_t x, unsigned n) { return (x >> n) | (x << (32 - n)); }

inline void sha256Block(uint32_t state[8], const uint8_t block[64])
{
  static const uint32_t k[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2};
  uint32_t w[64];
  for (unsigned i = 0; i < 16; ++i)
    w[i] = uint32_t(block[4 * i]) << 24 | uint32_t(block[4 * i + 1]) << 16 | uint32_t(block[4 * i + 2]) << 8 | block[4 * i + 3];
  for (unsigned i = 16; i < 64; ++i) {
    const uint32_t s0 = ror(w[i - 15], 7) ^ ror(w[i - 15], 18) ^ (w[i - 15] >> 3);
    const uint32_t s1 = ror(w[i - 2], 17) ^ ror(w[i - 2], 19) ^ (w[i - 2] >> 10);
    w[i] = w[i - 16] + s0 + w[i - 7] + s1;
  }
  uint32_t a = state[0], b = state[1], c = state[2], d = state[3], e = state[4], f = state[5], g = state[6], h = state[7];
  for (unsigned i = 0; i < 64; ++i) {
    const uint32_t s1 = ror(e, 6) ^ ror(e, 11) ^ ror(e, 25), ch = (e & f) ^ (~e & g);
    const uint32_t t1 = h + s1 + ch + k[i] + w[i];
    const uint32_t s0 = ror(a, 2) ^ ror(a, 13) ^ ror(a, 22), maj = (a & b) ^ (a & c) ^ (b & c);
    const uint32_t t2 = s0 + maj;
    h = g; g = f; f = e; e = d + t1; d = c; c = b; b = a; a = t1 + t2;
  }
  state[0] += a; state[1] += b; state[2] += c; state[3] += d; state[4] += e; state[5] += f; state[6] += g; state[7] += h;
}

inline bool sha256(const uint8_t *data, size_t size, uint8_t out[32])
{
  if ((!data && size) || !out) return false;
  uint32_t state[8] = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
  size_t at = 0;
  for (; size - at >= 64; at += 64) sha256Block(state, data + at);
  uint8_t tail[128] = {};
  const size_t rest = size - at;
  for (size_t i = 0; i < rest; ++i) tail[i] = data[at + i];
  tail[rest] = 0x80;
  const size_t count = rest < 56 ? 64 : 128;
  const uint64_t bits = uint64_t(size) * 8;
  for (unsigned i = 0; i < 8; ++i) tail[count - 1 - i] = uint8_t(bits >> (8 * i));
  sha256Block(state, tail);
  if (count == 128) sha256Block(state, tail + 64);
  for (unsigned i = 0; i < 8; ++i)
    for (unsigned j = 0; j < 4; ++j) out[4 * i + j] = uint8_t(state[i] >> (24 - 8 * j));
  return true;
}

// The image matches the digest the local packager wrote beside it.
inline bool imageIntact(const uint8_t *data, size_t size, const uint8_t *expected, size_t expectedLength)
{
  uint8_t got[32];
  if (!data || !size || !expected || expectedLength != 32 || !sha256(data, size, got)) return false;
  uint8_t different = 0;
  for (unsigned i = 0; i < 32; ++i) different |= got[i] ^ expected[i];
  return different == 0;
}

// Eight hex digits of a digest for the log line.
inline uint32_t digestPrefix(const uint8_t *digest, size_t length)
{
  return digest && length >= 4 ? uint32_t(digest[0]) << 24 | uint32_t(digest[1]) << 16 | uint32_t(digest[2]) << 8 | digest[3] : 0;
}

}  // namespace sheng_adsp
