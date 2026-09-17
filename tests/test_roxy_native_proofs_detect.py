from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_DIR / "scripts" / "roxy_native_register.py"


def _load_helpers():
    spec = importlib.util.spec_from_file_location("roxy_native_register", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["roxy_native_register"] = module
    spec.loader.exec_module(module)
    return module


class ProofsDetectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_helpers()

    def test_proofs_url(self) -> None:
        fn = self.mod._is_proofs_step_url_or_title
        self.assertTrue(
            fn(url="https://account.live.com/proofs/Add?mkt=EN-PH", title="")
        )

    def test_proofs_title(self) -> None:
        fn = self.mod._is_proofs_step_url_or_title
        self.assertTrue(fn(url="", title="Let's protect your account"))

    def test_signup_not_proofs(self) -> None:
        fn = self.mod._is_proofs_step_url_or_title
        self.assertFalse(
            fn(url="https://signup.live.com/signup", title="Create your password")
        )

    def test_registration_blocked_localized(self) -> None:
        fn = self.mod._is_registration_blocked

        class _UI:
            def __init__(self, title: str, url: str = "") -> None:
                self._title = title
                self._url = url

            def title(self) -> str:
                return self._title

            def url(self) -> str:
                return self._url

        self.assertTrue(fn(_UI("Nous avons rencontré un problème")))
        self.assertTrue(fn(_UI("We ran into a problem")))
        self.assertFalse(fn(_UI("Create your Microsoft account")))


if __name__ == "__main__":
    unittest.main()
