"""Tests for the first-run guided setup and its Settings entry point."""
import contextlib
import os
import sys
import types
import unittest

from test_plugin_ui import ADDON_SETTINGS, ROOT, _load_plugin

CLIENT_ID = "0123456789abcdef0123456789abcdef"


class _FakeDialog:
    """Answers scripted yes/no and input prompts and records every message."""

    answers = []
    inputs = []
    messages = []

    def yesno(self, heading, message, *args, **kwargs):
        self.messages.append(heading)
        return self.answers.pop(0)

    def ok(self, heading, message, *args, **kwargs):
        self.messages.append(heading)

    def input(self, heading, *args, **kwargs):
        self.messages.append(heading)
        return self.inputs.pop(0)

    def notification(self, *args, **kwargs):
        pass


class _FakeProgress:
    def create(self, *args, **kwargs):
        pass

    def update(self, *args, **kwargs):
        pass

    def iscanceled(self):
        return False

    def close(self):
        pass


class WizardHarness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT))
        cls.plugin = _load_plugin()
        cls.real_pairing = cls.plugin.guide_playback_pairing

    def setUp(self):
        ADDON_SETTINGS.clear()
        _FakeDialog.answers = []
        _FakeDialog.inputs = []
        _FakeDialog.messages = []
        self.state = {"credentials": False, "token": False, "client_id": CLIENT_ID, "signed_in": False}
        self.calls = []
        self.props = {}

    @contextlib.contextmanager
    def environment(self):
        state = self.state
        utils = types.ModuleType("utils")
        utils.migrate_legacy_addon_data = lambda: None
        utils.get_spotify_device_name = lambda: "Kodi-Resonance"
        utils.save_spotify_webapi_client_id = lambda value: ADDON_SETTINGS.__setitem__("spotify_webapi_client_id", value)
        utils.web_auth = lambda: types.SimpleNamespace(has_session=lambda: state.get("web_session", False))
        utils.resonance_signed_in = lambda: state["signed_in"]
        utils.spotty_has_credentials = lambda: state["credentials"]
        utils.get_cached_auth_token = lambda: "token" if state["token"] else ""
        utils.get_spotify_webapi_client_id = lambda: state["client_id"]
        saved_utils = sys.modules.get("utils")
        sys.modules["utils"] = utils
        plugin = self.plugin
        saved_gui = plugin.xbmcgui
        saved_pairing = plugin.guide_playback_pairing
        saved_login = plugin.web_login
        props = self.props
        window = types.SimpleNamespace(
            setProperty=lambda key, value: props.__setitem__(key, value),
            getProperty=lambda key: props.get(key, ""),
        )
        plugin.xbmcgui = types.SimpleNamespace(
            Dialog=_FakeDialog, DialogProgress=_FakeProgress, INPUT_ALPHANUM=0, Window=lambda *a, **k: window,
        )
        plugin.guide_playback_pairing = lambda *a, **k: (self.calls.append("pairing"), state["credentials"])[1]
        plugin.web_login = lambda *a, **k: self.calls.append("login")
        try:
            yield
        finally:
            plugin.xbmcgui = saved_gui
            plugin.guide_playback_pairing = saved_pairing
            plugin.web_login = saved_login
            if saved_utils is None:
                sys.modules.pop("utils", None)
            else:
                sys.modules["utils"] = saved_utils

class GuidedSetupTests(WizardHarness):
    def test_fresh_install_prompts(self):
        with self.environment():
            self.assertTrue(self.plugin.setup_prompt_needed())

    def test_fully_signed_in_does_not_prompt(self):
        self.state.update({"credentials": True, "token": True})
        with self.environment():
            self.assertFalse(self.plugin.setup_prompt_needed())

    def test_completed_wizard_still_prompts_when_not_fully_signed_in(self):
        ADDON_SETTINGS["setup_wizard_completed"] = "true"
        self.state.update({"credentials": True, "token": False})
        with self.environment():
            self.assertTrue(self.plugin.setup_prompt_needed())

    def test_saved_browser_session_counts_as_signed_in(self):
        self.state.update({"credentials": True, "token": False, "web_session": True})
        with self.environment():
            self.assertFalse(self.plugin.setup_prompt_needed())

    def test_prompt_is_asked_once_per_session(self):
        self.props["resonance.setup-prompted"] = "1"
        with self.environment():
            self.assertFalse(self.plugin.setup_prompt_needed())

    def test_skip_marks_done_without_steps(self):
        _FakeDialog.answers = [False]
        with self.environment():
            self.plugin.guided_setup()
        self.assertEqual(ADDON_SETTINGS["setup_wizard_completed"], "true")
        self.assertEqual(self.calls, [])

    def test_full_run_completes_all_steps(self):
        self.state["client_id"] = ""
        _FakeDialog.answers = [True]
        _FakeDialog.inputs = [CLIENT_ID]
        with self.environment():
            def finish_pairing(*a, **k):
                self.calls.append("pairing")
                self.state["credentials"] = True
                return True

            def finish_login(*a, **k):
                self.calls.append("login")
                self.state["token"] = True

            self.plugin.guide_playback_pairing = finish_pairing
            self.plugin.web_login = finish_login
            self.plugin.guided_setup()
        self.assertEqual(ADDON_SETTINGS["spotify_webapi_client_id"], CLIENT_ID)
        self.assertEqual(self.calls, ["pairing", "login"])
        self.assertEqual(_FakeDialog.messages[-1], "L11435", "the last dialog should be the setup summary")
        self.assertEqual(ADDON_SETTINGS["setup_wizard_completed"], "true")

    def test_invalid_client_id_stops_without_marking_done(self):
        self.state["client_id"] = ""
        _FakeDialog.answers = [True]
        _FakeDialog.inputs = ["not-a-client-id"]
        with self.environment():
            self.plugin.guided_setup()
        self.assertNotIn("spotify_webapi_client_id", ADDON_SETTINGS)
        self.assertNotIn("setup_wizard_completed", ADDON_SETTINGS)

    def test_unfinished_pairing_pauses_setup(self):
        _FakeDialog.answers = [True]
        with self.environment():
            self.plugin.guided_setup()
        self.assertEqual(self.calls, ["pairing"])
        self.assertNotIn("setup_wizard_completed", ADDON_SETTINGS)

    def test_pairing_during_wait_skips_retry_prompt(self):
        checks = {"count": 0}

        def paired_after_a_moment():
            checks["count"] += 1
            return checks["count"] > 2

        _FakeDialog.answers = [False]
        with self.environment():
            self.plugin.guide_playback_pairing = self.real_pairing
            sys.modules["utils"].spotty_has_credentials = paired_after_a_moment
            self.plugin.guide_playback_pairing()
        self.assertEqual(_FakeDialog.answers, [False], "Retry prompt was shown although the box was paired")

    def test_setup_flow_marker_is_set_during_wizard_and_cleared_after(self):
        seen = []
        original = _FakeDialog.yesno

        def record_marker(dialog, heading, *args, **kwargs):
            seen.append(self.props.get("resonance.setup-active", ""))
            return False

        _FakeDialog.yesno = record_marker
        try:
            with self.environment():
                self.plugin.guided_setup()
        finally:
            _FakeDialog.yesno = original
        self.assertEqual(seen, ["1"])
        self.assertEqual(self.props.get("resonance.setup-active", ""), "")

    def test_guided_setup_action_routes_to_wizard(self):
        _FakeDialog.answers = [False]
        with self.environment():
            self.plugin.PARAMS = {"action": "guided_setup"}
            self.plugin.HANDLE = -1
            self.plugin.run()
        self.assertEqual(ADDON_SETTINGS["setup_wizard_completed"], "true")


class NextStepAndResetTests(WizardHarness):
    def test_next_step_follows_the_first_missing_piece(self):
        cases = [
            ({"client_id": ""}, "guided_setup"),
            ({"credentials": False}, "guided_setup"),
            ({"credentials": True, "token": False}, "web_login"),
            ({"credentials": True, "token": True}, None),
        ]
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                self.state.update({"client_id": CLIENT_ID, "credentials": False, "token": False})
                self.state.update(overrides)
                with self.environment():
                    step = self.plugin.next_setup_step()
                self.assertEqual(step[1] if step else None, expected)

    def test_reset_moves_state_into_a_backup_and_keeps_the_marker(self):
        data = self.plugin.DATA
        os.makedirs(os.path.join(data, "runtime"), exist_ok=True)
        with open(os.path.join(data, "catalogue.db"), "w") as stream:
            stream.write("cache")
        with open(os.path.join(data, ".legacy-data-migrated"), "w") as stream:
            stream.write("plugin.audio.spotify2")
        ADDON_SETTINGS.update({"spotify_webapi_client_id": CLIENT_ID, "setup_wizard_completed": "true"})
        _FakeDialog.answers = [True]
        with self.environment():
            self.plugin.reset_resonance()
        self.assertFalse(os.path.exists(os.path.join(data, "catalogue.db")))
        self.assertFalse(os.path.exists(os.path.join(data, "runtime")))
        self.assertTrue(os.path.exists(os.path.join(data, ".legacy-data-migrated")))
        backups = [name for name in os.listdir(data) if name.startswith("reset-backup-")]
        self.assertEqual(len(backups), 1)
        self.assertTrue(os.path.exists(os.path.join(data, backups[0], "catalogue.db")))
        self.assertEqual(ADDON_SETTINGS["spotify_webapi_client_id"], "")
        self.assertEqual(ADDON_SETTINGS["setup_wizard_completed"], "false")

    def test_second_launch_does_not_start_another_setup(self):
        self.props["resonance.setup-active"] = "1"
        _FakeDialog.answers = [True]
        with self.environment():
            self.assertFalse(self.plugin.setup_prompt_needed())
            self.plugin.guided_setup()
        self.assertEqual(_FakeDialog.answers, [True], "a second setup asked its welcome question")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.props["resonance.setup-active"], "1")

    def test_prompt_later_changes_nothing(self):
        _FakeDialog.answers = [False]
        with self.environment():
            self.plugin.guided_setup(prompt=True)
        self.assertEqual(self.calls, [])
        self.assertNotIn("setup_wizard_completed", ADDON_SETTINGS)

    def test_prompt_finish_skips_the_welcome(self):
        _FakeDialog.answers = [True]
        with self.environment():
            self.plugin.guided_setup(prompt=True)
        self.assertEqual(self.calls, ["pairing"])
        self.assertEqual(_FakeDialog.answers, [])

    def test_reset_declined_changes_nothing(self):
        data = self.plugin.DATA
        with open(os.path.join(data, "catalogue.db"), "w") as stream:
            stream.write("cache")
        ADDON_SETTINGS["spotify_webapi_client_id"] = CLIENT_ID
        _FakeDialog.answers = [False]
        with self.environment():
            self.plugin.reset_resonance()
        self.assertTrue(os.path.exists(os.path.join(data, "catalogue.db")))
        self.assertEqual(ADDON_SETTINGS["spotify_webapi_client_id"], CLIENT_ID)
        os.remove(os.path.join(data, "catalogue.db"))


if __name__ == "__main__":
    unittest.main()
