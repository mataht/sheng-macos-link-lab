// SPDX-License-Identifier: GPL-3.0-or-later
// Host test of the login-path service logic: selector actions, DER device state, software key wrap, watchdog answers.
#include <cassert>
#include <cstdio>
#include <cstring>
#include <map>
#include <string>
#include <vector>

#include "../platform/sheng/macos26/login/aks_protocol.h"

using sheng_aks::Action;

static void check(bool condition, const char *what) {
  if (!condition) {
    std::printf("FAIL: %s\n", what);
    std::exit(1);
  }
}

// A deliberately small DER reader for the answer: SET of SEQUENCE{UTF8String key, INTEGER value}.
static std::map<std::string, uint64_t> parse_state(const uint8_t *p, size_t n) {
  std::map<std::string, uint64_t> out;
  check(n >= 2 && p[0] == 0x31, "state is a DER SET");
  size_t at = 2, total = p[1];
  if (p[1] == 0x81) { total = p[2]; at = 3; }
  check(at + total == n, "SET length matches");
  size_t end = at + total;
  while (at < end) {
    check(p[at] == 0x30, "entry is a SEQUENCE");
    size_t entry = at + 2 + p[at + 1], key = at + 2;
    check(p[key] == 0x0c, "key is UTF8String");
    std::string name((const char *)p + key + 2, p[key + 1]);
    size_t value_at = key + 2 + p[key + 1];
    check(p[value_at] == 0x02, "value is INTEGER");
    uint64_t value = 0;
    for (size_t i = 0; i < p[value_at + 1]; ++i) value = value << 8 | p[value_at + 2 + i];
    check(value_at + 2 + p[value_at + 1] == entry, "entry length matches");
    out[name] = value;
    at = entry;
  }
  return out;
}

int main() {
  // Selector actions: only the state selectors answer in stage 1.
  check(sheng_aks::action_for_selector(0, 1) == Action::Success, "handshake");
  check(sheng_aks::action_for_selector(7, 1) == Action::LockState, "lock state");
  check(sheng_aks::action_for_selector(14, 1) == Action::System, "system bag");
  check(sheng_aks::action_for_selector(17, 1) == Action::DeviceState && sheng_aks::action_for_selector(35, 1) == Action::DeviceState, "device state");
  check(sheng_aks::action_for_selector(40, 1) == Action::Generation, "generation");
  for (uint32_t s : {4u, 9u, 13u, 26u, 27u, 55u, 100u, 101u, 160u})
    check(sheng_aks::action_for_selector(s, 1) == Action::Success, "bookkeeping selectors succeed");
  check(sheng_aks::action_for_selector(10, 1) == Action::Unsupported && sheng_aks::action_for_selector(11, 1) == Action::Unsupported &&
            sheng_aks::action_for_selector(23, 1) == Action::Unsupported, "stage 1 has no key wrap");
  check(sheng_aks::action_for_selector(10, 2) == Action::Wrap && sheng_aks::action_for_selector(11, 2) == Action::Unwrap &&
            sheng_aks::action_for_selector(23, 2) == Action::BagUuid, "stage 2 has key wrap");
  for (uint32_t s : {1u, 2u, 3u, 6u, 8u, 15u, 74u, 121u, 125u, 999u})
    check(sheng_aks::action_for_selector(s, 2) == Action::Unsupported, "other selectors are unsupported");

  // Device state: every key the libaks decoder reads, unlocked (sls 0) and the override value.
  uint8_t der[192];
  size_t n = sheng_aks::encode_device_state(der, sizeof der, 0);
  check(n > 0, "encodes");
  auto state = parse_state(der, n);
  check(state.at("ss") == 4 && state.at("sls") == 0, "unlocked state");
  for (const char *key : {"sb", "sfa", "sgs", "sas", "sgpe", "srcd", "sms", "ses", "bh"})
    check(state.count(key) == 1 && state.at(key) == 0, "zero fields present");
  check(parse_state(der, sheng_aks::encode_device_state(der, sizeof der, 3)).at("sls") == 3, "lock state override");
  check(sheng_aks::encode_device_state(der, 16, 0) == 0, "short buffer reports 0");

  // SHA-256 known answer, then the key wrap round trip and its authentication.
  uint8_t digest[32];
  sheng_aks::Sha256 sha;
  sha.update("abc", 3);
  sha.final(digest);
  const uint8_t expected[32] = {0xba, 0x78, 0x16, 0xbf, 0x8f, 0x01, 0xcf, 0xea, 0x41, 0x41, 0x40, 0xde, 0x5d, 0xae, 0x22, 0x23,
                                0xb0, 0x03, 0x61, 0xa3, 0x96, 0x17, 0x7a, 0x9c, 0xb4, 0x10, 0xff, 0x61, 0xf2, 0x00, 0x15, 0xad};
  check(!std::memcmp(digest, expected, 32), "sha256 abc");

  const uint8_t id[] = "00000000-1111-2222-3333-444444444444";
  uint8_t key[32];
  sheng_aks::device_key(id, sizeof id - 1, key);
  uint8_t plain[40], blob[sheng_aks::kTagBytes + 40], back[40];
  for (unsigned i = 0; i < 40; ++i) plain[i] = (uint8_t)(i * 7 + 1);
  sheng_aks::wrap_key(key, plain, 40, blob);
  check(sheng_aks::unwrap_key(key, blob, sizeof blob, back) && !std::memcmp(plain, back, 40), "wrap round trip");
  check(std::memcmp(blob + sheng_aks::kTagBytes, plain, 40) != 0, "payload is not plain");
  blob[10] ^= 1;
  check(!sheng_aks::unwrap_key(key, blob, sizeof blob, back), "tampered blob is refused");

  uint8_t other[32];
  sheng_aks::device_key((const uint8_t *)"other", 5, other);
  check(std::memcmp(key, other, 32) != 0, "the identity changes the key");
  uint8_t uuid[16];
  sheng_aks::bag_uuid(key, uuid);
  check(uuid[0] || uuid[1], "bag uuid derived");

  // Watchdog: the daemon must read monitoring disabled and its check-in must succeed.
  using sheng_watchdog::Action;
  check(sheng_watchdog::action_for_selector(sheng_watchdog::kSelCheckEnabled) == Action::ReportDisabled, "check enabled");
  check(sheng_watchdog::action_for_selector(sheng_watchdog::kSelDefanged) == Action::ReportZero, "defanged");
  for (uint32_t s : {1u, 2u, 3u, 4u, 6u, 7u, 8u})
    check(sheng_watchdog::action_for_selector(s) == Action::Accept, "accepted selectors");
  check(sheng_watchdog::action_for_selector(9) == Action::ReportIOKitOff, "iokit monitoring");
  check(sheng_watchdog::action_for_selector(10) == Action::Unsupported, "selector count is 10");

  std::printf("PASS native login host tests\n");
  return 0;
}
