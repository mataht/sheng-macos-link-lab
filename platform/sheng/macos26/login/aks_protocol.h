// SPDX-License-Identifier: BSD-2-Clause-Patent
// Answers of the default-off AppleKeyStore for a system without Secure Enclave: no keybag, no passcode, nothing locked.
//
// Pure logic (no IOKit), shared with the host test. The selector numbers, the DER device state and the software key wrap
// come from UniXNU/AppleKeyStoreState NativeKeyStoreEvents.cpp, commit 58e5461ed3b751e4c91426b72d8686419abe9550 (the
// Copyright notice and license are in NativeKeyStoreEvents-LICENSE next to this file). That source was verified on
// iOS/iPadOS only. Offline checks against macOS 26.5 25F71: AppleKeyStore.framework uses the same selector numbers
// (aks_get_lock_state 7, aks_unlock_device 9, aks_lock_bag 13, aks_get_system 14, aks_get_device_state 17,
// aks_assert_hold 26, aks_assert_drop 27, aks_unload_session_bags 55, aks_assert_promote 100, aks_assert_consume 101),
// the selector 0 handshake, and the DER keys ss sls sb sfa sgs srcd sms ses. Nothing here protects data.
//
// Stage 1 (sheng-aks=1): state answers only. Stage 2 (sheng-aks=2): also the software key wrap of the upstream driver,
// keyed from the platform identity so that items wrapped in one boot unwrap in the next.
#pragma once
#include <stddef.h>
#include <stdint.h>

namespace sheng_aks {

enum class Action {
  Unsupported,  // every selector not listed: the caller gets kUnsupported, as with an unreachable service
  Success,      // handshake and bag/assertion/notification bookkeeping: no outputs
  System,       // scalar out: the system keybag handle
  LockState,    // scalar in: handle; scalar out: bit 2 = unlocked since boot
  DeviceState,  // struct out: DER SET of {key, value}
  Generation,   // scalars in: bag, operation; scalar out: generation 0
  Wrap,         // stage 2
  Unwrap,       // stage 2
  BagUuid,      // stage 2
};

constexpr uint32_t kSelHandshake = 0, kSelLockState = 7, kSelWrapKey = 10, kSelUnwrapKey = 11, kSelGetSystem = 14,
                   kSelDeviceState = 17, kSelBagUuid = 23, kSelExtendedDeviceState = 35, kSelGeneration = 40,
                   kSelCacheFlow = 160;
constexpr uint64_t kUnlockedSinceBoot = 4;
constexpr uint32_t kStateFlags = 4;
constexpr uint64_t kSystemHandle = 0;
constexpr size_t kTagBytes = 8, kMaxKeyBytes = 256;
constexpr uint32_t kMaxStage = 2;

inline Action action_for_selector(uint32_t selector, uint32_t stage) {
  switch (selector) {
    // Selector 0 runs right after IOServiceOpen in libaks: a failure drops the connection and reopens on every call.
    case kSelHandshake: case 4: case 5: case 9: case 12: case 13: case 26: case 27: case 55: case 100: case 101: case 102:
    case kSelCacheFlow:
      return Action::Success;
    case kSelGetSystem: return Action::System;
    case kSelLockState: return Action::LockState;
    case kSelDeviceState: case kSelExtendedDeviceState: return Action::DeviceState;
    case kSelGeneration: return Action::Generation;
    case kSelWrapKey: return stage >= 2 ? Action::Wrap : Action::Unsupported;
    case kSelUnwrapKey: return stage >= 2 ? Action::Unwrap : Action::Unsupported;
    case kSelBagUuid: return stage >= 2 ? Action::BagUuid : Action::Unsupported;
    default: return Action::Unsupported;
  }
}

// Self-contained so wrapping does not depend on when the kernel's crypto provider registers.
class Sha256 {
  uint32_t h[8] = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
  uint8_t buffer[64];
  uint64_t total = 0;
  size_t fill = 0;
  static uint32_t rotate(uint32_t v, unsigned n) { return (v >> n) | (v << (32 - n)); }
  void block(const uint8_t *p) {
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
      w[i] = (uint32_t)p[i * 4] << 24 | (uint32_t)p[i * 4 + 1] << 16 | (uint32_t)p[i * 4 + 2] << 8 | p[i * 4 + 3];
    for (unsigned i = 16; i < 64; ++i) {
      uint32_t s0 = rotate(w[i - 15], 7) ^ rotate(w[i - 15], 18) ^ (w[i - 15] >> 3);
      uint32_t s1 = rotate(w[i - 2], 17) ^ rotate(w[i - 2], 19) ^ (w[i - 2] >> 10);
      w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    uint32_t s[8];
    for (unsigned i = 0; i < 8; ++i) s[i] = h[i];
    for (unsigned i = 0; i < 64; ++i) {
      uint32_t t1 = s[7] + (rotate(s[4], 6) ^ rotate(s[4], 11) ^ rotate(s[4], 25)) + ((s[4] & s[5]) ^ (~s[4] & s[6])) +
                    k[i] + w[i];
      uint32_t t2 = (rotate(s[0], 2) ^ rotate(s[0], 13) ^ rotate(s[0], 22)) + ((s[0] & s[1]) ^ (s[0] & s[2]) ^ (s[1] & s[2]));
      for (unsigned j = 7; j > 0; --j) s[j] = s[j - 1];
      s[4] += t1;
      s[0] = t1 + t2;
    }
    for (unsigned i = 0; i < 8; ++i) h[i] += s[i];
  }

public:
  void update(const void *data, size_t n) {
    const uint8_t *p = (const uint8_t *)data;
    total += n;
    while (n) {
      if (!fill && n >= sizeof buffer) { block(p); p += sizeof buffer; n -= sizeof buffer; continue; }
      size_t take = sizeof buffer - fill < n ? sizeof buffer - fill : n;
      for (size_t i = 0; i < take; ++i) buffer[fill + i] = p[i];
      fill += take; p += take; n -= take;
      if (fill == sizeof buffer) { block(buffer); fill = 0; }
    }
  }
  void final(uint8_t out[32]) {
    uint64_t bits = total * 8;
    const uint8_t one = 0x80, zero = 0;
    update(&one, 1);
    while (fill != 56) update(&zero, 1);
    uint8_t length[8];
    for (unsigned i = 0; i < 8; ++i) length[i] = (uint8_t)(bits >> (56 - 8 * i));
    update(length, 8);
    for (unsigned i = 0; i < 8; ++i)
      for (unsigned j = 0; j < 4; ++j) out[i * 4 + j] = (uint8_t)(h[i] >> (24 - 8 * j));
  }
};

// The same identity gives the same key at every boot, or items wrapped earlier could not be opened again.
inline void device_key(const uint8_t *identity, size_t identity_size, uint8_t key[32]) {
  static const char domain[] = "UniXNU AppleKeyStore v1";
  Sha256 hash;
  hash.update(domain, sizeof domain - 1);
  hash.update(identity, identity_size);
  hash.final(key);
}

inline void wrap_tag(const uint8_t key[32], const uint8_t *plain, size_t n, uint8_t tag[kTagBytes]) {
  uint8_t digest[32];
  const uint8_t label = 'T';
  Sha256 hash;
  hash.update(key, 32); hash.update(&label, 1); hash.update(plain, n); hash.final(digest);
  for (size_t i = 0; i < kTagBytes; ++i) tag[i] = digest[i];
}

inline void wrap_stream(const uint8_t key[32], const uint8_t tag[kTagBytes], const uint8_t *in, uint8_t *out, size_t n) {
  const uint8_t label = 'S';
  for (size_t at = 0; at < n; at += 32) {
    uint8_t counter[4] = {(uint8_t)(at >> 5), (uint8_t)(at >> 13), (uint8_t)(at >> 21), (uint8_t)(at >> 29)};
    uint8_t digest[32];
    Sha256 hash;
    hash.update(key, 32); hash.update(&label, 1); hash.update(tag, kTagBytes); hash.update(counter, 4); hash.final(digest);
    for (size_t i = 0; i < 32 && at + i < n; ++i) out[at + i] = in[at + i] ^ digest[i];
  }
}

// blob must hold kTagBytes + n. n is 1..kMaxKeyBytes.
inline void wrap_key(const uint8_t key[32], const uint8_t *plain, size_t n, uint8_t *blob) {
  wrap_tag(key, plain, n, blob);
  wrap_stream(key, blob, plain, blob + kTagBytes, n);
}

// Returns false when the blob does not authenticate. n is the blob size (more than kTagBytes).
inline bool unwrap_key(const uint8_t key[32], const uint8_t *blob, size_t n, uint8_t *plain) {
  n -= kTagBytes;
  wrap_stream(key, blob, blob + kTagBytes, plain, n);
  uint8_t tag[kTagBytes];
  wrap_tag(key, plain, n, tag);
  uint8_t difference = 0;
  for (size_t i = 0; i < kTagBytes; ++i) difference |= (uint8_t)(tag[i] ^ blob[i]);
  return difference == 0;
}

inline void bag_uuid(const uint8_t key[32], uint8_t out[16]) {
  uint8_t digest[32];
  const uint8_t label = 'U';
  Sha256 hash;
  hash.update(key, 32); hash.update(&label, 1); hash.final(digest);
  for (unsigned i = 0; i < 16; ++i) out[i] = digest[i];
}

struct DerWriter {
  uint8_t *p; size_t cap, n = 0; bool ok = true;
  void put(uint8_t b) { if (n < cap) p[n++] = b; else ok = false; }
  void len(size_t l) { if (l < 0x80) put((uint8_t)l); else { put(0x81); put((uint8_t)l); } }  // entries stay below 256 bytes
};

// Smallest two's-complement big-endian INTEGER content for a non-negative value.
inline size_t der_integer_content(uint64_t v, uint8_t out[9]) {
  uint8_t raw[8];
  for (int i = 0; i < 8; ++i) raw[i] = (uint8_t)(v >> (56 - 8 * i));
  size_t start = 0;
  while (start < 7 && raw[start] == 0) ++start;
  size_t n = 0;
  if (raw[start] & 0x80) out[n++] = 0;
  while (start < 8) out[n++] = raw[start++];
  return n;
}

inline size_t key_length(const char *key) { size_t n = 0; while (key[n]) ++n; return n; }

// DER SET of SEQUENCE{UTF8String, INTEGER}. Returns the length, or 0 when the buffer is too small.
inline size_t encode_device_state(uint8_t *out, size_t capacity, uint32_t lock_state) {
  struct Entry { const char *key; uint64_t value; };
  const Entry entries[] = {{"ss", kStateFlags}, {"sls", lock_state}, {"sb", 0}, {"sfa", 0}, {"sgs", 0}, {"sas", 0},
                           {"sgpe", 0}, {"srcd", 0}, {"sms", 0}, {"ses", 0}, {"bh", 0}};
  size_t total = 0;
  for (const Entry &e : entries) {
    uint8_t c[9];
    total += 2 + (2 + key_length(e.key)) + (2 + der_integer_content(e.value, c));
  }
  DerWriter w{out, capacity};
  w.put(0x31); w.len(total);
  for (const Entry &e : entries) {
    uint8_t c[9];
    size_t kl = key_length(e.key), il = der_integer_content(e.value, c);
    w.put(0x30); w.len((2 + kl) + (2 + il));
    w.put(0x0c); w.put((uint8_t)kl); for (size_t i = 0; i < kl; ++i) w.put((uint8_t)e.key[i]);
    w.put(0x02); w.put((uint8_t)il); for (size_t i = 0; i < il; ++i) w.put(c[i]);
  }
  return w.ok ? w.n : 0;
}

}  // namespace sheng_aks

namespace sheng_watchdog {

// Offline check against the 25F71 AppleARMWatchdogTimer dispatch table and watchdogd callers.
enum class Action {
  Unsupported,
  ReportDisabled,  // scalar out: 0 (kIOWatchdogDaemonCheckEnabled: watchdogd then logs, sleeps 2 s and exits 0)
  ReportZero,      // scalar out: 0 (CheckUserspaceDefanged)
  Accept,          // Checkin, Disable/ReenableUserspaceMonitoring, the Test selectors, panic request: accepted, no effect
  ReportIOKitOff,  // struct out: 2 bytes, both 0 (CheckIOKitMonitoringEnabled)
};
constexpr uint32_t kSelCheckEnabled = 0, kSelCheckin = 1, kSelPanicDevice = 2, kSelDisable = 3, kSelReenable = 4,
                   kSelDefanged = 5, kSelTestCpuHang = 6, kSelTestSocWatchdog = 7, kSelTestIOKitBusy = 8,
                   kSelIOKitMonitoring = 9, kSelectorCount = 10;

inline Action action_for_selector(uint32_t selector) {
  switch (selector) {
    case kSelCheckEnabled: return Action::ReportDisabled;
    case kSelDefanged: return Action::ReportZero;
    case kSelCheckin: case kSelPanicDevice: case kSelDisable: case kSelReenable:
    case kSelTestCpuHang: case kSelTestSocWatchdog: case kSelTestIOKitBusy:
      return Action::Accept;
    case kSelIOKitMonitoring: return Action::ReportIOKitOff;
    default: return Action::Unsupported;
  }
}

}  // namespace sheng_watchdog
