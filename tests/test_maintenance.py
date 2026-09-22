"""Exercise maintenance against disposable userdata, without a Kodi session."""
import ast
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))
SOURCE = ROOT / "resources/lib/utils.py"
TREE = ast.parse(SOURCE.read_text())


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.ns = {"os": os, "ADDON_DATA_PATH": str(self.path),
                   "RUNTIME_PATH": str(self.path / "runtime"),
                   "LEGACY_RUNTIME_PATH": str(self.path / "runtime-v2")}
        for name in ("delete_persistent_spotify_profile", "clear_spotify_account_email_hint",
                     "clear_spotify_account_info", "cache_auth_token",
                     "cache_auth_token_expires_at", "cache_auth_client_id",
                     "set_spotify_connection_status", "terminate_process"):
            self.ns[name] = Mock()
        functions = [node for node in TREE.body if isinstance(node, ast.FunctionDef)
                     and node.name in ("delete_resonance_credentials", "clear_resonance_cache")]
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(SOURCE), "exec"), self.ns)

    def test_logout_removes_vault_and_runtime_backups_but_keeps_settings(self):
        names = ["spotify-credentials.json", "spotify-credentials.json.tmp", "settings.xml"]
        for folder in ("runtime", "runtime-v2", "spotty-cache"):
            names += [folder + "/" + name for name in
                      ("credentials.json", "credentials.json.bak", "spotty-token", "spotty-token.bak", "spotty-token.tmp")]
        for name in names:
            target = self.path / name
            target.parent.mkdir(exist_ok=True)
            target.write_text("fixture")
        self.ns["delete_resonance_credentials"]()
        self.assertEqual([p.name for p in self.path.rglob("*") if p.is_file()], ["settings.xml"])
        self.ns["cache_auth_token"].assert_called_once_with("")
        self.ns["set_spotify_connection_status"].assert_called_once_with(False)

    def test_cache_clear_preserves_login_and_updates_existing_connection(self):
        for name in ("spotify-credentials.json", "settings.xml", "spotify-api-gate.json"):
            (self.path / name).write_text("fixture")
        db = sqlite3.connect(self.path / "catalogue.db")
        self.addCleanup(db.close)
        db.execute("CREATE TABLE pages (key TEXT PRIMARY KEY, value TEXT, expires REAL)")
        db.execute("CREATE TABLE gates (provider TEXT PRIMARY KEY, until REAL)")
        db.execute("INSERT INTO pages VALUES ('search:test', '[]', 9999999999)")
        db.execute("INSERT INTO gates VALUES ('spotify', 9999999999)")
        db.commit()
        self.ns["clear_resonance_cache"]()
        self.assertEqual(db.execute("SELECT key, value FROM pages").fetchall(), [("migration-complete", "true")])
        self.assertEqual(db.execute("SELECT * FROM gates").fetchall(), [])
        self.assertTrue((self.path / "spotify-credentials.json").exists())
        self.assertTrue((self.path / "settings.xml").exists())
        self.assertFalse((self.path / "spotify-api-gate.json").exists())

    def test_service_logout_clears_catalogue_cache_after_credentials(self):
        source = (ROOT / "service.py").read_text()
        credentials = source.index("utils.delete_resonance_credentials()")
        cache = source.index("utils.clear_resonance_cache()")
        self.assertLess(credentials, cache)

    def test_permission_errors_are_reported(self):
        with patch.object(os, "remove", side_effect=PermissionError("denied")):
            for name in ("delete_resonance_credentials", "clear_resonance_cache"):
                with self.subTest(name=name), self.assertRaises(PermissionError):
                    self.ns[name]()
