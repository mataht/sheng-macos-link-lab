// SPDX-License-Identifier: GPL-3.0-or-later
// Host mangling check: a concrete subclass of the generated AppleUSBXHCI declarations, built without implementations so that
// its undefined references are exactly the virtual slots the subclass inherits (compared against the contract).
#include "OriginalUsbXhciABI.h"

class Concrete final : public AppleUSBXHCI {
  OSDeclareDefaultStructors(Concrete)
};

OSDefineMetaClassAndStructors(Concrete, AppleUSBXHCI)
