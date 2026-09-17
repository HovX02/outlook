from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "roxy_register.py"
SPEC = spec_from_file_location("roxy_register", SCRIPT)
MODULE = module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class RoxyRegisterStateTests(unittest.TestCase):
    def test_classifies_post_signup_interstitials(self):
        classify = MODULE._classify_ms_page
        self.assertEqual(classify("https://privacynotice.account.microsoft.com/notice", ""), "privacy")
        self.assertEqual(classify("https://login.microsoft.com/consumers/fido/create", ""), "passkey")
        self.assertEqual(classify("https://login.live.com/login.srf", "Stay signed in?"), "kmsi")
        self.assertEqual(classify("https://account.microsoft.com/", ""), "account_home")
        self.assertEqual(classify("https://outlook.live.com/mail/0/", ""), "outlook")
        self.assertEqual(classify("https://signup.live.com/signup", "Let's prove you're human"), "captcha")

    def test_classifies_oauth_states(self):
        classify = MODULE._classify_ms_page
        self.assertEqual(classify("https://account.live.com/Consent/Update", ""), "consent")
        self.assertEqual(
            classify("https://login.live.com/", "Are you trying to sign in to Thunderbird?"),
            "oauth_continue",
        )
        self.assertEqual(classify("http://localhost/?code=abc", ""), "callback")

    def test_click_consent_tries_yes_before_accept(self):
        clicked_names: list[str] = []

        class FakeButton:
            def __init__(self, name: str, visible: bool) -> None:
                self._name = name
                self._visible = visible

            @property
            def first(self):
                return self

            def count(self):
                return 1 if self._visible else 0

            def is_visible(self):
                return self._visible

            def is_enabled(self):
                return self._visible

            def click(self, timeout=0):
                clicked_names.append(self._name)

        class FakePage:
            url = "https://account.live.com/Consent/Update"

            class _Req:
                def post(self, *args, **kwargs):
                    raise RuntimeError("test: skip http")

            request = _Req()

            def bring_to_front(self):
                return None

            def wait_for_load_state(self, state, timeout=0):
                return None

            def content(self):
                return '"sCanary":"abc","sClientId":"cid","sRawInputScopes":"scope"'

            def wait_for_timeout(self, ms):
                return None

            def get_by_role(self, role, name=None, exact=False):
                return FakeButton(name or "", name == "Yes")

            def get_by_text(self, text, exact=False):
                return FakeButton(text, text == "Yes")

            def locator(self, selector):
                return FakeButton("", False)

            def evaluate(self, script):
                return False

        page = FakePage()
        self.assertTrue(MODULE._click_consent(page))
        self.assertIn("Yes", clicked_names)

    def test_submit_consent_http_parses_config(self):
        html = (
            '"sCanary":"abc123","sClientId":"9e5f94bc-e8a4-4e73-b8be-63364c29d753",'
            '"sRawInputScopes":"Mail.ReadWrite Mail.Send"'
        )
        self.assertTrue(MODULE._is_consent_page(html, "https://account.live.com/Consent/Update"))
        self.assertEqual(MODULE._config_str(html, "sCanary"), "abc123")
        self.assertIn("9e5f94bc", MODULE._config_str(html, "sClientId"))

    def test_classifies_creation_block(self):
        self.assertEqual(
            MODULE._classify_ms_page(
                "https://signup.live.com/signup", "Account creation has been blocked"
            ),
            "blocked",
        )


if __name__ == "__main__":
    unittest.main()
