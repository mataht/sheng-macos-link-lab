# Imported keyboard cover files

Source: https://github.com/UniXNU/UniXNU-sheng, commit a68ee4ee67cc71792fbf519de7e3e28072c717b5, license BSD-2-Clause-Patent (`LICENSE` in this directory).

Copied unchanged from `package/`: `GeniI2c.c`, `GeniI2c.h`, `Wn8030.c`, `Wn8030.h`, `QcomRpmh.c`, `QcomRpmh.h`, and from `package/tests/`: `geni_i2c_host.c`, `wn8030_host.c`.

Upstream reports this code working on iPadOS 27.0.1 (typing and the touchpad on the keyboard cover). That is upstream's claim; it has not been observed on macOS 26. The macOS front end is `../native_keyboard.cpp`.

The controller RAM image is a vendor file and is not in this repository. A local package carries it in the device tree.
