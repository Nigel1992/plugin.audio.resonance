"""Release notes that Resonance shows after an update.

The notes are plain text with short lines so they stay readable in the Kodi
text dialog on every skin, on a TV and with the small fonts of the default
skin. Every version that adds something new gets its own entry; the notes of
a version list everything that changed since the release before it, so a
user who skips several releases still sees the full picture.
"""

NOTES = {
    "1.2.2": """NEW
  * Account and status shows one row per topic. Tap the row to
    pair playback or sign in.
  * After pairing, browser sign-in starts straight away if needed.
  * The home menu shows the next setup step.
  * Menus, dialogs and notifications are translated: English,
    Dutch, German, Spanish, French and Hebrew.

FIXED
  * Playing without sign-in shows a clear message.
  * The old Set up Spotify route asks for the Client ID first.
  * Setup texts that were only in English are translated.

""",

    "1.2.1": """NEW
  * Guided setup asks to finish setup when Resonance is not
    fully signed in. Run it again from Settings > Account.
  * Each setup step reports its result in a dialog.
  * Reset Resonance in Settings > Account. A backup is kept.
  * Home and Account and status show the next setup step.

FIXED
  * Pairing during setup no longer starts playback.
  * A stalled Spotify Connect stream restarts its receiver.
  * Repeated pairing rejections restart the pairing receiver.
  * The Spotify Client ID survives a lost settings value.
  * A second setup does not start while one is open.
  * No busy spinner left after the setup sign-in step.

""",

    "1.2.0": """NEW
  * Spotify Connect audio now works on LibreELEC (Linux ARM64) too,
    streaming through Kodi's VideoPlayer.
  * Rebuilt Linux spotty helper with the Connect audio bridge.

FIXED - SETUP NAVIGATION
  * Fixed a busy spinner left over after Spotify
    setup or browser authorization.
  * Login dialogs run after the menu has finished
    loading.
  * Release notes and internal settings updates
    no longer interrupt menu creation.
  * Fixed Spotify setup and Android playback
    authentication.

ANDROID
  * Added Spotify Connect audio for ARM64 and ARMv7
    Android devices.
  * Use Kodi's streaming player to avoid live audio
    stuttering.

""",

    "1.1.8": """FIXED - SETUP NAVIGATION
  * Fixed a busy spinner left over after Spotify
    setup or browser authorization.
  * Login dialogs run after the menu has finished
    loading.
  * Release notes and internal settings updates
    no longer interrupt menu creation.""",
    "1.1.7": """NEW - 32-BIT ANDROID
  * Added the rebuilt playback and Spotify Connect
    helper for Android ARMv7 devices.
  * Uses the same streaming player fixes as Shield.""",
    "1.1.6": """FIXED - SPOTIFY CONNECT
  * Connect uses Kodi's streaming player to avoid
    the stuttering heard with its music player.
  * Improved audio pipe reading.
  * Song title and artwork update after playback
    starts.""",
    "1.1.5": """FIXED - SPOTIFY CONNECT
  * Fixed audio stopping shortly after selecting
    Kodi in Spotify Desktop.
  * Kodi's extra stream requests no longer cut
    off the main audio reader.""",
    "1.1.4": """NEW - SPOTIFY CONNECT ON ANDROID ARM64
  * Select Kodi-Resonance in Spotify Desktop or
    mobile to play through Kodi's speakers.
  * Control play, pause, songs, seek and volume
    from Spotify.
  * Song title and cover art appear in Kodi.
  * Kodi pause, resume and stop control the
    Connect session. Starting another Kodi item
    releases Spotify Connect.

  Your existing pairing and Liked Songs remain.""",
    "1.1.3": """FIXED
  * Spotify setup opens the pairing dialog again.
  * Android pairing uses the authentication helper.
  * Shield / Android ARM64 track playback uses a
    rebuilt helper with compatible Spotify login.
  * Playback helper errors are now logged.

PLAYBACK
  * Select songs inside Resonance after setup.
    The device in Spotify's picker is for pairing.""",
    "1.1.0": """NEW
  * Your Spotify Library page with Liked Songs,
    Saved Albums, Followed Artists and your playlists
  * Like and unlike tracks, save and remove albums
  * Add tracks to your own playlists
  * Create playlists from the library page or from
    the track menu
  * Remove a track from a playlist you own
  * Unfollow a playlist or an artist
  * An icon on every menu row
  * This window. It shows what changed after an
    update. Open it again any time from
    Account and status -> What's new.

FIXED
  * "Remove from Spotify playlist" now always
    removes the track from the playlist you are in
  * The context menu of a track works again:
    "Add to Spotify playlist" and "Create playlist
    with this track" did nothing at all
  * The artist menu works again: everything,
    top songs, albums, singles and appears on
  * Playback status rows only show information,
    only Quality and Connect react to a click
  * Album pages no longer fail with "Invalid limit"

NOTE
  * Choose "Reauthorize Spotify" under Account and
    status once, so Spotify grants the new library
    and playlist permissions.""",
}


def notes_for(version):
    """Return the release notes for ``version``.

    An unknown version returns an empty string, so the caller can decide
    whether a window makes sense at all.
    """
    return NOTES.get(str(version or "").strip(), "")


def has_notes(version):
    """Return True when release notes exist for ``version``."""
    return bool(notes_for(version))
