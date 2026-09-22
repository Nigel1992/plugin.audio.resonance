"""Resonance's lightweight Spotipy package entry point.

Resonance supplies a ready access token and uses only ``Spotify`` during normal
Kodi navigation.  Importing every optional OAuth/cache helper eagerly pulled
in browser and HTTP-server modules on every fresh Kodi ARM invoker.  Preserve
the public Spotipy attributes through PEP 562 lazy lookup while keeping the
normal client path small.
"""

from .exceptions import *  # noqa: F401,F403
from .client import *  # noqa: F401,F403


# Spotify tightened GET /v1/artists/{id}/albums to a maximum page size of 10.
# Keep the compatibility rule at the bundled Spotipy boundary so Resonance's
# existing offset-page collector can continue to assemble the configured Kodi
# browse page (20/40/60/80/100) from multiple API requests without changing
# limits for unrelated endpoints.  When the UI asks for the combined
# album+single branch, include compilations as part of that release view.
_resonance_artist_albums = Spotify.artist_albums


def _resonance_artist_albums_limit_compat(
    self, artist_id, album_type=None, include_groups=None, country=None, limit=20, offset=0
):
    try:
        api_limit = max(1, min(int(limit), 10))
    except (TypeError, ValueError):
        api_limit = 10

    groups = include_groups
    if isinstance(groups, str):
        normalized = [part.strip() for part in groups.split(",") if part.strip()]
        if normalized == ["album", "single"]:
            normalized.append("compilation")
            groups = ",".join(normalized)

    return _resonance_artist_albums(
        self,
        artist_id,
        album_type=album_type,
        include_groups=groups,
        country=country,
        limit=api_limit,
        offset=offset,
    )


Spotify.artist_albums = _resonance_artist_albums_limit_compat


_LAZY_EXPORT_MODULES = {
    "CacheHandler": "cache_handler",
    "CacheFileHandler": "cache_handler",
    "DjangoSessionCacheHandler": "cache_handler",
    "FlaskSessionCacheHandler": "cache_handler",
    "MemoryCacheHandler": "cache_handler",
    "MemcacheCacheHandler": "cache_handler",
    "SpotifyClientCredentials": "oauth2",
    "SpotifyOAuth": "oauth2",
    "SpotifyOauthError": "oauth2",
    "SpotifyStateError": "oauth2",
    "SpotifyImplicitGrant": "oauth2",
    "SpotifyPKCE": "oauth2",
    "CLIENT_CREDS_ENV_VARS": "util",
    "prompt_for_user_token": "util",
}


def __getattr__(name):
    module_name = _LAZY_EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(f"{__name__}.{module_name}"), name)
    globals()[name] = value
    return value
