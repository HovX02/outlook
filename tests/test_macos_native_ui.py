import subprocess
import unittest

from outlook_api_reg.macos_native_ui import NativeUIError, RoxyNativeUI


class FakeRunner:
    def __init__(self, *, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, self.returncode, self.stdout, self.stderr)


class RoxyNativeUITests(unittest.TestCase):
    def test_type_text_passes_value_as_argument(self):
        runner = FakeRunner()
        ui = RoxyNativeUI(action_delay=0, runner=runner)
        ui.type_text('A!b@example.com "quoted"')
        command, kwargs = runner.calls[0]
        self.assertEqual(command[0], "swift")
        self.assertTrue(command[1].endswith("tools/macos_type_text.swift"))
        self.assertNotIn("A!b@example.com", " ".join(command))
        self.assertEqual(kwargs["input"], 'A!b@example.com "quoted"')

    def test_press_maps_supported_keys(self):
        runner = FakeRunner()
        ui = RoxyNativeUI(action_delay=0, runner=runner)
        ui.press("Return")
        self.assertEqual(runner.calls[0][0][-1], "36")

    def test_navigate_passes_url_as_data(self):
        runner = FakeRunner()
        ui = RoxyNativeUI(action_delay=0, runner=runner)
        ui.navigate("https://signup.live.com/signup?a=1&b=2")
        command = runner.calls[0][0]
        self.assertEqual(command[-3], "--")
        self.assertEqual(command[-2], "RoxyChrome")
        self.assertEqual(command[-1], "https://signup.live.com/signup?a=1&b=2")

    def test_click_relative_passes_integer_coordinates(self):
        class BoundsRunner(FakeRunner):
            def __call__(self, command, **kwargs):
                self.calls.append((command, kwargs))
                stdout = "10, 20, 936, 788" if command[0] == "osascript" else ""
                return subprocess.CompletedProcess(command, 0, stdout, "")

        runner = BoundsRunner()
        ui = RoxyNativeUI(action_delay=0, runner=runner)
        ui.click_relative(460, 412)
        command = runner.calls[1][0]
        self.assertEqual(command[-2:], ["470", "432"])

    def test_wait_for_url_accepts_matching_active_tab(self):
        runner = FakeRunner(stdout="https://account.live.com/proofs/Add")
        ui = RoxyNativeUI(action_delay=0, runner=runner)
        self.assertIn("/proofs/", ui.wait_for_url(("account.live.com/proofs",), timeout=0.1))

    def test_osascript_error_is_explained(self):
        runner = FakeRunner(stderr="not authorized", returncode=1)
        ui = RoxyNativeUI(action_delay=0, runner=runner)
        with self.assertRaisesRegex(NativeUIError, "not authorized"):
            ui.activate()


if __name__ == "__main__":
    unittest.main()
