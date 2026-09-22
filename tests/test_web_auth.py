import importlib.util
import json
import pathlib
import tempfile
import time
import unittest
from unittest import mock

import urllib.error
import urllib.request

PATH = pathlib.Path(__file__).parents[1] / "resources/lib/web_auth.py"
spec = importlib.util.spec_from_file_location("web_auth", PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class CredentialsResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def json(self):
        return self._payload

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class ErrorResponse:
    def __init__(self, code, body=b""):
        self.code, self.body = code, body

    def read(self):
        return self.body

    def close(self):
        pass


class Flaky:
    def __enter__(self):
        raise urllib.error.URLError("boom")

    def __exit__(self, *args):
        return False


class TestPkce(unittest.TestCase):
    def test_pair_matches(self):
        verifier, challenge = module.pkce_pair()
        import base64
        import hashlib
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).rstrip(b"=").decode("ascii")
        self.assertEqual(challenge, expected)

    def test_authorize_url_shape(self):
        uri = module.authorize_url("a" * 32, "http://x/cb", ["playlist-read-private", "user-read-private"], "ch", "st")
        self.assertTrue(uri.startswith(module.AUTHORIZE_URL + "?"))
        self.assertIn("client_id=" + "a" * 32, uri)
        self.assertIn("code_challenge=ch", uri)
        self.assertIn("code_challenge_method=S256", uri)
        self.assertIn("redirect_uri=http%3A%2F%2Fx%2Fcb", uri)
        self.assertIn("scope=playlist-read-private+user-read-private", uri)
        self.assertIn("state=st", uri)
        self.assertIn("show_dialog=true", uri)


class TestShorten(unittest.TestCase):
    def test_short_url_taken(self):
        self.assertEqual(
            module.shorten_url("https://example.com/very/long", fetcher=lambda url, clicks: {"short_url": "https://zip1.io/AbC12"}),
            "https://zip1.io/AbC12",
        )

    def test_falls_back_on_bad_payload(self):
        self.assertEqual(module.shorten_url("x", fetcher=lambda url, clicks: {}), "")

    def test_raises_returns_empty(self):
        def boom(url, clicks):
            raise OSError("no network")
        self.assertEqual(module.shorten_url("x", fetcher=boom), "")


class TestExchange(unittest.TestCase):
    def test_exchange_code_parses(self):
        payload = {"access_token": "tok1", "refresh_token": "ref1", "expires_in": 3600}
        with mock.patch.object(module.urllib.request, "urlopen", return_value=CredentialsResponse(payload)) as urlopen:
            result = module.exchange_code("c" * 32, "http://x/cb", "code9", "ver9")
        self.assertEqual(result["access_token"], "tok1")
        body = urlopen.call_args[0][0].data
        self.assertIn(b"grant_type=authorization_code", body)
        self.assertIn(b"code_verifier=ver9", body)

    def test_exchange_code_http_error(self):
        with mock.patch.object(module.urllib.request, "urlopen", side_effect=urllib.error.HTTPError("u", 400, "bad", None, ErrorResponse(400, b"oops"))):
            with self.assertRaises(module.WebAuthError):
                module.exchange_code("c" * 32, "http://x/cb", "code9", "ver9")

    def test_refresh_access_token(self):
        payload = {"access_token": "tok2", "expires_in": 3600}
        with mock.patch.object(module.urllib.request, "urlopen", return_value=CredentialsResponse(payload)) as urlopen:
            result = module.refresh_access_token("c" * 32, "ref1")
        self.assertEqual(result["access_token"], "tok2")
        body = urlopen.call_args[0][0].data
        self.assertIn(b"grant_type=refresh_token", body)
        self.assertIn(b"refresh_token=ref1", body)


PATCHED = {"urllib.request": module.urllib.request, "urllib.error": module.urllib.error}


class TestWebAuthStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = module.WebAuth(self.dir, "c" * 32, clock=time.monotonic)

    def test_save_load_roundtrip(self):
        data = {"access_token": "tok", "refresh_token": "ref", "expires_at": 123, "client_id": "c" * 32}
        self.store.save(data)
        self.assertEqual(self.store.load(), data)

    def test_client_id_mismatch_ignored(self):
        data = {"access_token": "tok", "refresh_token": "ref", "expires_at": 123, "client_id": "other"}
        self.store.save(data)
        self.assertIsNone(self.store.load())

    def test_has_session(self):
        self.assertFalse(self.store.has_session())
        self.store.save({"access_token": "tok", "refresh_token": "ref", "expires_at": 123, "client_id": "c" * 32})
        self.assertTrue(self.store.has_session())

    def test_clear(self):
        self.store.save({"access_token": "tok", "refresh_token": "ref", "expires_at": 123, "client_id": "c" * 32})
        self.store.clear()
        self.assertFalse(self.store.has_session())

    def test_access_token_unexpired(self):
        self.store.save({"access_token": "tok", "refresh_token": "ref", "expires_at": 10 ** 9, "client_id": "c" * 32})
        self.assertEqual(self.store.access_token(), "tok")

    def test_access_token_refreshes_when_expired(self):
        self.store.save({"access_token": "old", "refresh_token": "ref", "expires_at": -1, "client_id": "c" * 32})
        fetched = {"access_token": "new", "refresh_token": "ref", "expires_in": 3600}

        def fetcher(client_id, refresh):
            self.assertEqual(client_id, "c" * 32)
            self.assertEqual(refresh, "ref")
            return fetched

        self.assertEqual(self.store.access_token(refresh_fetcher=fetcher), "new")
        stored = self.store.load()
        self.assertEqual(stored["access_token"], "new")
        self.assertGreater(stored["expires_at"], self.store.clock())

    def test_access_token_no_refresh_returns_empty(self):
        self.store.save({"access_token": "old", "refresh_token": "", "expires_at": -1, "client_id": "c" * 32})
        with mock.patch.object(module, "refresh_access_token", side_effect=AssertionError("must not refresh")):
            self.assertEqual(self.store.access_token(), "")


class TestRelayPolling(unittest.TestCase):
    def test_loopback_is_local(self):
        self.assertTrue(module.is_local_redirect("http://127.0.0.1:53209/callback"))
        self.assertTrue(module.is_local_redirect("http://localhost:53209/callback"))
        self.assertFalse(module.is_local_redirect("https://myapp.vercel.app/api/callback"))

    def test_derive_poll_url(self):
        self.assertEqual(module.derive_poll_url("https://myapp.vercel.app/api/callback"),
                         "https://myapp.vercel.app/api/poll")
        self.assertEqual(module.derive_poll_url("http://127.0.0.1:53209/callback"), "")
        self.assertEqual(module.derive_poll_url("garbage"), "")

    def test_poll_for_code(self):
        def stub(url, **kwargs):
            self.assertIn("state=state1", url)
            return CredentialsResponse({"code": "AQ-code"})
        with mock.patch.object(module.urllib.request, "urlopen", side_effect=stub) as urlopen:
            self.assertEqual(module.poll_for_code("https://myapp.vercel.app/api/poll", "state1"), "AQ-code")
        self.assertIn("state=state1", urlopen.call_args[0][0])

    def test_poll_waits_when_no_code(self):
        def stub(url, **kwargs):
            return CredentialsResponse({"waiting": True})
        with mock.patch.object(module.urllib.request, "urlopen", side_effect=stub):
            self.assertEqual(module.poll_for_code("https://myapp.vercel.app/api/poll", "st"), "")

    def test_poll_returns_empty_on_network_error(self):
        with mock.patch.object(module.urllib.request, "urlopen", side_effect=urllib.error.URLError("boom")):
            self.assertEqual(module.poll_for_code("https://myapp.vercel.app/api/poll", "st"), "")


class TestCallbackServer(unittest.TestCase):
    def test_callback_captures_code_and_state(self):
        server = module.CallbackServer(port=0)
        server.start()
        try:
            port = server.httpd.server_port
            url = "http://127.0.0.1:%d/callback?code=abc&state=xyz" % port
            with urllib.request.urlopen(url, timeout=5) as response:
                self.assertEqual(response.status, 200)
            result = server.wait(timeout=5)
            self.assertEqual(result.get("code"), "abc")
            self.assertEqual(result.get("state"), "xyz")
        finally:
            server.stop()

    def test_callback_reports_error(self):
        server = module.CallbackServer(port=0)
        server.start()
        try:
            port = server.httpd.server_port
            with urllib.request.urlopen("http://127.0.0.1:%d/callback?error=access_denied&state=zz" % port, timeout=5):
                pass
            result = server.wait(timeout=5)
            self.assertEqual(result.get("error"), "access_denied")
        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main()