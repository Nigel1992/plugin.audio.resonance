import ast
from pathlib import Path
import re
import unittest
from unittest.mock import Mock
import xml.etree.ElementTree as ET

ROOT = Path(__file__).parents[1]


class ClientIdTests(unittest.TestCase):
    def test_custom_id_required_no_bundled_default(self):
        source = ROOT / "resources/lib/utils.py"
        tree = ast.parse(source.read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                        and n.name == "get_spotify_webapi_client_id")
        addon = Mock()
        api = Mock()
        api.Addon.return_value = addon
        ns = {"re": re, "xbmcaddon": api, "ADDON_ID": "test"}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), ns)
        for value, expected in (("  " + "AB" * 16 + "  ", "ab" * 16),
                                ("", ""), ("partial", ""), ("2" * 32, "2" * 32)):
            addon.getSetting.return_value = value
            self.assertEqual(ns["get_spotify_webapi_client_id"](), expected)
        addon.setSetting.assert_not_called()

    def test_no_bundled_client_id_constant_in_code(self):
        source = (ROOT / "resources/lib/utils.py").read_text()
        self.assertNotIn("RESONANCE_DEFAULT_WEBAPI_CLIENT_ID", source)
        self.assertNotIn("LEGACY_SPOTIFY_WEBAPI_CLIENT_ID", source)
        self.assertNotIn("2eb96f9b37494be1824999d58028a305", source)

    def test_client_id_editor_visible_in_advanced_settings(self):
        root = ET.parse(ROOT / "resources/settings.xml")
        setting = root.find('.//category[@id="advanced"]//setting[@id="spotify_webapi_client_id"]')
        self.assertIsNotNone(setting)
        self.assertEqual(setting.find("control").get("type"), "edit")
        self.assertTrue(setting.findtext("constraints/allowempty"))

    def test_redirect_uri_is_mandatory_with_vercel_default(self):
        root = ET.parse(ROOT / "resources/settings.xml")
        setting = root.find('.//category[@id="advanced"]//setting[@id="web_auth_redirect_uri"]')
        self.assertEqual(setting.findtext("constraints/allowempty"), "false")
        self.assertEqual(setting.findtext("default"),
                         "https://resonance-spotify-callback.vercel.app/api/callback")

    def test_playlist_spotty_fallback_setting_removed(self):
        root = ET.parse(ROOT / "resources/settings.xml")
        setting = root.find('.//category[@id="advanced"]//setting[@id="playlist_spotty_fallback"]')
        self.assertIsNone(setting)

    def test_settings_have_single_setup_button(self):
        root = ET.parse(ROOT / "resources/settings.xml")
        account = root.find('.//category[@id="account"]')
        actions = account.findall('.//setting[@type="action"]')
        self.assertEqual([setting.get("id") for setting in actions], ["setup_spotify"])
        self.assertEqual(actions[0].findtext("data"), "RunPlugin(plugin://plugin.audio.resonance/?action=setup)")
        self.assertIsNone(account.find('.//setting[@id="connect_spotify"]'))
        self.assertIsNone(account.find('.//setting[@id="web_login_spotify"]'))

    def test_cached_auth_token_falls_back_to_web_api_session(self):
        source = ROOT / "resources/lib/utils.py"
        tree = ast.parse(source.read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                        and n.name == "get_cached_auth_token")
        cache = {}

        class WebSession:
            def has_session(self):
                return True

            def access_token(self):
                return "web-token"

            def load(self):
                return {"expires_at": 5000}

        def cache_value(key, value):
            cache[key] = value

        ns = {
            "Exception": Exception,
            "LOGWARNING": 0,
            "KODI_PROPERTY_SPOTIFY_AUTH_TOKEN": "token",
            "get_cached_value_from_kodi": lambda key: cache.get(key),
            "get_spotify_webapi_client_id": lambda: "a" * 32,
            "get_cached_auth_client_id": lambda: cache.get("client-id", ""),
            "get_cached_auth_token_expires_at": lambda: cache.get("expires", ""),
            "web_auth": lambda: WebSession(),
            "cache_auth_token": lambda value: cache_value("token", value),
            "cache_auth_token_expires_at": lambda value: cache_value("expires", value),
            "cache_auth_client_id": lambda value: cache_value("client-id", value),
            "log_msg": Mock(),
            "time": Mock(time=Mock(return_value=1000)),
        }
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), ns)
        self.assertEqual(ns["get_cached_auth_token"](), "web-token")
        self.assertEqual(cache["token"], "web-token")
        self.assertEqual(cache["client-id"], "a" * 32)

    def test_web_api_session_wins_over_cached_spotty_token(self):
        source = ROOT / "resources/lib/utils.py"
        tree = ast.parse(source.read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                        and n.name == "get_cached_auth_token")
        cache = {"token": "spotty-token", "client-id": "a" * 32, "expires": "5000"}

        class WebSession:
            def has_session(self):
                return True

            def access_token(self):
                return "web-token"

            def load(self):
                return {"expires_at": 5000}

        def cache_value(key, value):
            cache[key] = value

        ns = {
            "Exception": Exception,
            "LOGWARNING": 0,
            "KODI_PROPERTY_SPOTIFY_AUTH_TOKEN": "token",
            "get_cached_value_from_kodi": lambda key: cache.get(key),
            "get_spotify_webapi_client_id": lambda: "a" * 32,
            "get_cached_auth_client_id": lambda: cache.get("client-id", ""),
            "get_cached_auth_token_expires_at": lambda: cache.get("expires", ""),
            "web_auth": lambda: WebSession(),
            "cache_auth_token": lambda value: cache_value("token", value),
            "cache_auth_token_expires_at": lambda value: cache_value("expires", value),
            "cache_auth_client_id": lambda value: cache_value("client-id", value),
            "log_msg": Mock(),
            "time": Mock(time=Mock(return_value=1000)),
        }
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), ns)
        self.assertEqual(ns["get_cached_auth_token"](), "web-token")
        self.assertEqual(cache["token"], "web-token")

    def test_catalogue_token_never_falls_back_to_spotty_cache(self):
        source = ROOT / "resources/lib/utils.py"
        tree = ast.parse(source.read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                        and n.name == "get_cached_auth_token")
        cache = {"token": "spotty-token", "client-id": "a" * 32, "expires": "5000"}

        class WebSession:
            def has_session(self):
                return False

        ns = {
            "Exception": Exception,
            "LOGWARNING": 0,
            "KODI_PROPERTY_SPOTIFY_AUTH_TOKEN": "token",
            "get_cached_value_from_kodi": lambda key: cache.get(key),
            "get_spotify_webapi_client_id": lambda: "a" * 32,
            "web_auth": lambda: WebSession(),
            "log_msg": Mock(),
            "time": Mock(time=Mock(return_value=1000)),
        }
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), ns)
        self.assertEqual(ns["get_cached_auth_token"](), "")
