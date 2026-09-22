"""Compatibility wrapper for the v1.2.18.22 authentication helper.

v1.2.18.24 retains the v1.2.18.23 bounded credentials-settle check and one
explicit token retry, while importing Kodi log constants from xbmc correctly.
Credentials are never deleted when token creation temporarily fails.
"""

import json
import os
import time

from xbmc import LOGINFO, LOGWARNING

from spotty_auth_impl import SpottyAuth as _BaseSpottyAuth
from utils import log_msg


class SpottyAuth(_BaseSpottyAuth):
    def __init__(self, spotty):
        super().__init__(spotty)
        self._resonance_spotty = spotty

    def _credentials_stable(self, settle_seconds: float = 0.12) -> bool:
        path = self._resonance_spotty.get_spotty_credentials_file()
        if not path or not os.path.isfile(path):
            return False
        try:
            first_stat = os.stat(path)
            if first_stat.st_size < 32:
                return False
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if not isinstance(payload, dict):
                return False
            username = str(payload.get("username") or "").strip()
            auth_data = payload.get("auth_data") or payload.get("authData") or ""
            if not username or not auth_data:
                return False
            time.sleep(max(0.0, float(settle_seconds)))
            second_stat = os.stat(path)
            return (
                first_stat.st_size == second_stat.st_size
                and first_stat.st_mtime_ns == second_stat.st_mtime_ns
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False

    def renew_token(self, quiet: bool = False) -> str:
        # The optional service warm-up must not race an in-progress Device
        # Connect write. API-backed navigation will retry on demand later.
        if quiet and not self._credentials_stable(0.08):
            log_msg(
                "AUTH_DIAG state=token_restore_deferred "
                "reason=credentials_not_stable credentials_preserved=true",
                LOGINFO,
            )
            return ""

        if not quiet:
            deadline = time.monotonic() + 1.25
            while time.monotonic() < deadline:
                if self._credentials_stable(0.10):
                    break
                time.sleep(0.05)

        token = super().renew_token(quiet=quiet)
        if token or quiet:
            return token

        # Legacy Spotty may finish Device Connect just before credentials are
        # reusable by --save-token on Windows. Retry exactly once after a
        # bounded settle interval. The original failure path already preserves
        # credentials; this wrapper never removes them.
        time.sleep(0.35)
        if not self._credentials_stable(0.12):
            log_msg(
                "AUTH_DIAG state=token_retry_skipped "
                "reason=credentials_not_stable credentials_preserved=true",
                LOGWARNING,
            )
            return ""

        log_msg(
            "AUTH_DIAG state=token_retry_started retry=1 "
            "reason=no_access_token credentials_preserved=true",
            LOGINFO,
        )
        token = super().renew_token(quiet=False)
        if token:
            log_msg(
                "AUTH_DIAG state=token_retry_succeeded retry=1 ",
                LOGINFO,
            )
        else:
            log_msg(
                "AUTH_DIAG state=token_retry_failed retry=1 "
                "credentials_preserved=true",
                LOGWARNING,
            )
        return token
