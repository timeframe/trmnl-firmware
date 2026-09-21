#include <wifi_retry_policy.h>

WifiRetryAction wifiRetryAction(uint8_t retryCount) { return {.buttonOnly = retryCount > 3}; }