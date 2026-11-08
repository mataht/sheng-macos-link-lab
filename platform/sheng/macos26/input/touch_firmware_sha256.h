// SPDX-License-Identifier: GPL-3.0-or-later
// Portable SHA-256 for the small, fixed, local-only NT36532 firmware. This
// avoids trusting an ADT digest label and adds no unpublished kernel imports.
#pragma once
#include <stddef.h>
#include <stdint.h>
namespace sheng { namespace touch_firmware {
constexpr size_t kFirmwareSize = 245760;
static const uint8_t kExpected[32] = {
  0x75,0x05,0x08,0xbb,0x4d,0x75,0x47,0x2d,0x35,0xce,0x58,0x52,0xec,0x84,0x70,0xd6,
  0x6a,0xfa,0x6b,0xf4,0xa4,0x27,0x4c,0x13,0x6b,0x37,0x59,0x03,0x42,0x60,0xef,0x59
};
inline uint32_t ror(uint32_t x, unsigned n) { return (x >> n) | (x << (32-n)); }
inline void block(uint32_t h[8], const uint8_t b[64]) {
  static const uint32_t k[64] = {
    0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
    0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
    0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
    0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
    0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
    0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
    0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
    0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2
  };
  uint32_t w[64];
  for (unsigned i=0;i<16;++i) w[i]=(uint32_t(b[4*i])<<24)|(uint32_t(b[4*i+1])<<16)|(uint32_t(b[4*i+2])<<8)|b[4*i+3];
  for (unsigned i=16;i<64;++i) {
    const uint32_t s0=ror(w[i-15],7)^ror(w[i-15],18)^(w[i-15]>>3);
    const uint32_t s1=ror(w[i-2],17)^ror(w[i-2],19)^(w[i-2]>>10);
    w[i]=w[i-16]+s0+w[i-7]+s1;
  }
  uint32_t a=h[0],c=h[2],b0=h[1],d=h[3],e=h[4],f=h[5],g=h[6],h0=h[7];
  for(unsigned i=0;i<64;++i) {
    const uint32_t s1=ror(e,6)^ror(e,11)^ror(e,25), ch=(e&f)^((~e)&g);
    const uint32_t t1=h0+s1+ch+k[i]+w[i];
    const uint32_t s0=ror(a,2)^ror(a,13)^ror(a,22), maj=(a&b0)^(a&c)^(b0&c);
    const uint32_t t2=s0+maj;
    h0=g;g=f;f=e;e=d+t1;d=c;c=b0;b0=a;a=t1+t2;
  }
  h[0]+=a;h[1]+=b0;h[2]+=c;h[3]+=d;h[4]+=e;h[5]+=f;h[6]+=g;h[7]+=h0;
}
inline bool digest(const uint8_t *data,size_t size,uint8_t out[32]) {
  if ((!data && size) || !out || uint64_t(size)>UINT64_MAX/8) return false;
  uint32_t h[8]={0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19};
  size_t off=0;
  while(size-off>=64) {block(h,data+off);off+=64;}
  uint8_t tail[128]={};const size_t rem=size-off;
  for(size_t i=0;i<rem;++i) tail[i]=data[off+i];
  tail[rem]=0x80; const size_t count=rem<56?64:128;
  const uint64_t bits=uint64_t(size)*8;
  for(unsigned i=0;i<8;++i) tail[count-1-i]=uint8_t(bits>>(8*i));
  block(h,tail);if(count==128) block(h,tail+64);
  for(unsigned i=0;i<8;++i) for(unsigned j=0;j<4;++j) out[4*i+j]=uint8_t(h[i]>>(24-8*j));
  return true;
}
inline bool accepted(const uint8_t *data,size_t size) {
  if(size!=kFirmwareSize) return false;
  uint8_t got[32]; if(!digest(data,size,got)) return false;
  uint8_t different=0;for(unsigned i=0;i<32;++i) different|=got[i]^kExpected[i];
  return different==0;
}
}}
