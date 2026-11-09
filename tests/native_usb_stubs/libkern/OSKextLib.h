#pragma once
#include <IOKit/IOService.h>
typedef int OSReturn;
extern "C" OSReturn OSKextLoadKextWithIdentifier(const char *);
