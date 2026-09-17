from __future__ import annotations

import unittest
from unittest.mock import Mock

from outlook_api_reg.roxy_browser import (
    RoxyBrowserClient,
    finger_info_for_country,
    proxy_info_from_line,
)


class RoxyBrowserClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = RoxyBrowserClient("http://127.0.0.1:50000", "secret", 42)
        self.response = Mock()
        self.response.content = b"{}"
        self.response.raise_for_status.return_value = None
        self.client.session.request = Mock(return_value=self.response)

    def test_open_returns_cdp_websocket(self) -> None:
        self.response.json.return_value = {"code": 0, "data": {"ws": "ws://127.0.0.1/devtools/browser/1"}}
        ws = self.client.open("profile-1")
        self.assertEqual(ws, "ws://127.0.0.1/devtools/browser/1")
        kwargs = self.client.session.request.call_args.kwargs
        self.assertEqual(kwargs["json"]["workspaceId"], 42)
        self.assertEqual(kwargs["json"]["dirId"], "profile-1")
        self.assertEqual(kwargs["headers"]["token"], "secret")

    def test_list_profiles_normalizes_rows(self) -> None:
        self.response.json.return_value = {
            "code": 0,
            "data": {"rows": [{"dirId": "abc", "windowName": "Outlook", "remark": "smoke"}]},
        }
        profiles = self.client.list_profiles()
        self.assertEqual([(p.id, p.name, p.remark) for p in profiles], [("abc", "Outlook", "smoke")])

    def test_api_error_is_raised(self) -> None:
        self.response.json.return_value = {"code": 500, "msg": "bad token"}
        with self.assertRaisesRegex(RuntimeError, "bad token"):
            self.client.open("profile-1")

    def test_finger_info_follow_ip_omits_language(self) -> None:
        info = finger_info_for_country("US", follow_ip=True)
        self.assertTrue(info["isLanguageBaseIp"])
        self.assertNotIn("language", info)

    def test_finger_info_country_explicit_uses_bcp47(self) -> None:
        info = finger_info_for_country("US", follow_ip=False)
        self.assertEqual(info["language"], "en-US")
        self.assertEqual(info["displayLanguage"], "en-US")
        self.assertFalse(info["isLanguageBaseIp"])
        self.assertEqual(info["timeZone"], "America/New_York")

    def test_finger_info_ph_locale(self) -> None:
        info = finger_info_for_country("PH", follow_ip=False)
        self.assertEqual(info["language"], "en-PH")

    def test_update_profile_proxy_sends_country_locale(self) -> None:
        self.response.json.return_value = {"code": 0, "data": {}}
        self.client.resolve_workspace_id = Mock(return_value=42)  # type: ignore[method-assign]
        self.client.update_profile_proxy("profile-1", "127.0.0.1:8080:user:pass", country="US")
        body = self.client.session.request.call_args.kwargs["json"]
        self.assertIn("proxyInfo", body)
        self.assertIn("fingerInfo", body)
        self.assertEqual(body["fingerInfo"]["language"], "en-US")
        self.assertFalse(body["fingerInfo"]["isLanguageBaseIp"])

    def test_update_profile_proxy_retries_without_finger_info(self) -> None:
        self.client.resolve_workspace_id = Mock(return_value=42)  # type: ignore[method-assign]

        def side_effect(*_args, **_kwargs):
            if self.client.session.request.call_count == 1:
                self.response.json.return_value = {"code": 500, "msg": "language参数值错误"}
                return self.response
            self.response.json.return_value = {"code": 0, "data": {}}
            return self.response

        self.client.session.request = Mock(side_effect=side_effect)
        self.client.update_profile_proxy("profile-1", "127.0.0.1:8080:user:pass", country="US")
        calls = self.client.session.request.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertNotIn("fingerInfo", calls[1].kwargs["json"])

    def test_proxy_info_from_line_http(self) -> None:
        info = proxy_info_from_line("us.ipwo.net:7878:user:secret")
        self.assertEqual(info["host"], "us.ipwo.net")
        self.assertEqual(info["port"], "7878")
        self.assertEqual(info["proxyCategory"], "HTTP")


if __name__ == "__main__":
    unittest.main()
