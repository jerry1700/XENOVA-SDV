"""고장 감시 설정과 관측 만료를 분리하는 작은 회귀다. 실보드 시간 보증은 아니다."""

import os
import unittest
from unittest.mock import patch

from xenova_vehicle.runtime.health_policy import ConfigurationError, HealthPolicy, health_policy


class HealthPolicyTests(unittest.TestCase):
    def tearDown(self):
        health_policy.cache_clear()

    def test_defaults_and_once_per_process_settings(self):
        health_policy.cache_clear()
        with patch.dict(os.environ, {'XENOVA_JETSON_COMPUTE_STALL_S': '20.5'}, clear=True):
            configured = health_policy()
            self.assertEqual(configured.compute_stall_us, 20_500_000)
            self.assertEqual(configured.control_stall_us, HealthPolicy().control_stall_us)
        self.assertIs(health_policy(), configured)

    def test_invalid_values_are_explicit_configuration_failures_before_resources_start(self):
        for value in ('NaN', 'inf', '-1', '0', '0.0000001', '1e308', 'invalid'):
            with self.subTest(value=value), patch.dict(os.environ,
                    {'XENOVA_JETSON_CONTROL_STALL_S': value}, clear=True):
                health_policy.cache_clear()
                with self.assertRaises(ConfigurationError):
                    health_policy()
