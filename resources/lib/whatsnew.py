"""Release notes that Resonance shows after an update.

The notes are plain text with short lines so they stay readable in the Kodi
text dialog on every skin, on a TV and with the small fonts of the default
skin. Every version that adds something new gets its own entry; the notes of
a version list everything that changed since the release before it, so a
user who skips several releases still sees the full picture.
"""

NOTES = {
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
