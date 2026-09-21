#pragma once

#include <stdint.h>

struct WifiRetryAction {
  bool buttonOnly;
};

WifiRetryAction wifiRetryAction(uint8_t retryCount);