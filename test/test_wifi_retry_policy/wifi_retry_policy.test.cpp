#include <unity.h>
#include <wifi_retry_policy.h>

void test_transient_wifi_outage_keeps_automatic_recovery_enabled(void) {
  TEST_ASSERT_FALSE_MESSAGE(wifiRetryAction(4).buttonOnly,
                            "retry exhaustion disables timer wake and can leave stale content displayed indefinitely");
}

void setUp(void) {}
void tearDown(void) {}

int main(int argc, char **argv) {
  UNITY_BEGIN();
  RUN_TEST(test_transient_wifi_outage_keeps_automatic_recovery_enabled);
  return UNITY_END();
}