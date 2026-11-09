// SPDX-License-Identifier: GPL-3.0-or-later
// Pulls in the real driver source and gives the host test a way to call NativeAdsp::start.
#include "native_adsp.cpp"
bool hostStartNativeAdsp()
{
  NativeAdsp *adsp = new NativeAdsp;
  return adsp->start(nullptr);
}
