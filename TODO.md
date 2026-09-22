# Resonance Roadmap / TODO

Planned and requested features that are not implemented yet.

## Browsing
- [ ] **Browse an artist's best / top songs** — per-artist "top tracks" list so
      selecting an artist shows their most played songs without extra search.
- [ ] Browse an artist's albums, singles and "appears on" collections.
- [ ] Related artists navigation from an artist row.
- [ ] Combined "everything for artist" page (top tracks + albums + singles +
      appears on) in one browseable directory.
- [ ] Consistent context-menu access to the artist browsing actions above from
      every track/album/artist row.

## Catalogue token ideas (Web API via the PKCE browser token)
Verify current Spotify Web API endpoint availability and scopes before
building any of these.

- [ ] **Similar artists** — `/artists/{id}/related-artists` from an artist row.
- [ ] **Radio / recommendations by seeds** — `/recommendations` with
      artist/track/genre seeds and tunable targets.
- [ ] **Genre seeds list** — `/recommendations/available-genre-seeds`.
- [ ] **Audio features per track** — `/audio-features/{id}?ids=` in context menus.
- [ ] *Needs new scopes (opt-in re-sign-in):* `/me/top/*`, `/me/player/recently-played`,
      `/me/library` (+ save/remove), `/me/following`, `POST /me/playlists` +
      `/playlists/{id}/items` editing, `/me/player` playback control and
      currently-playing.

## Library / playback
- [ ] Re-enable the disabled write actions ("save to my music", "add to
      playlist", follow/unfollow) once a stable Spotify API path is available
      (see Known limitations in README).

## Platform
- [ ] Validate Android ARMv7/AArch64.
- [ ] Add and validate separate playback helper binaries before advertising
      Windows, macOS or Linux x86/x64 support.
