from __future__ import annotations

import unittest

from outlook_api_reg.proxy_utils import rewrite_ipwo_zone_country


class IpwoZoneRewriteTests(unittest.TestCase):
    def test_global_to_us(self) -> None:
        raw = "us.ipwo.net:7878:testuser_custom_zone_GLOBAL_sid_78751344_time_10:testpass"
        out = rewrite_ipwo_zone_country(raw, "US")
        self.assertIn("custom_zone_US", out)
        self.assertNotIn("GLOBAL", out)

    def test_global_to_gb(self) -> None:
        raw = "us.ipwo.net:7878:user_custom_zone_GLOBAL_sid_1_time_10:secret"
        out = rewrite_ipwo_zone_country(raw, "GB")
        self.assertIn("custom_zone_GB", out)

    def test_plain_proxy_unchanged(self) -> None:
        raw = "gate.example.com:1000:user:pass"
        self.assertEqual(rewrite_ipwo_zone_country(raw, "US"), raw)


if __name__ == "__main__":
    unittest.main()
