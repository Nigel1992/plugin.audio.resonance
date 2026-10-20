<p align="center">
  <a href="https://discord.gg/UbPRh74PzV">
    <img alt="Join our Discord" src="https://img.shields.io/badge/Join%20our%20Discord-5865F2?style=for-the-badge&logo=discord&logoColor=white" />
  </a>
</p>


# Resonance for Kodi

![Resonance](resources/logo.png)

**Resonance plays Spotify Premium music through Kodi.** It browses Spotify
playlists, search results, albums, artists and links, then streams tracks
through bundled Spotty/librespot playback helpers.

| Quick Facts | |
| --- | --- |
| Spotify account | Premium required |
| Supported target | LibreELEC on Raspberry Pi ARM/AArch64 |
| Experimental target | Android ARMv7/AArch64 |
| Not supported | Windows, macOS, Linux x86/x64 |
| Catalogue | Regular Spotify Web API with browser PKCE token |
| Playback | Spotify Device Connect with Spotty/librespot |
| Version | 1.0.3 |

| | |
| --- | --- |
| [![License](https://img.shields.io/github/license/Nigel1992/plugin.audio.resonance)](LICENSE.txt) | [![Release](https://img.shields.io/github/v/release/Nigel1992/plugin.audio.resonance?sort=semver)](https://github.com/Nigel1992/plugin.audio.resonance/releases) |
| [![Stars](https://img.shields.io/github/stars/Nigel1992/plugin.audio.resonance?style=social&label=Star)](https://github.com/Nigel1992/plugin.audio.resonance) | [![Python](https://img.shields.io/badge/language-Python%203-3776AB.svg)](https://www.python.org/) |

## Quick Start

1. Download `plugin.audio.resonance-1.0.3.zip` from the [Releases page](https://github.com/Nigel1992/plugin.audio.resonance/releases).
2. In Kodi, install it with **Add-ons -> Install from zip file**.
3. Restart Kodi once.
4. Open **Add-ons -> Music add-ons -> Resonance**.
5. Create a Spotify Developer app, add the redirect URI below, and paste its Client ID into **Settings -> Advanced**.
6. Use **Set up Spotify**. Resonance pairs playback first, then shows a QR code and URL for browser sign-in.

```text
https://resonance-spotify-callback.vercel.app/api/callback
```

## Features

- Search Spotify tracks, albums, artists and playlists.
- Browse your Spotify playlists, including private playlists available to your account.
- Browse album tracks, playlist tracks and artist albums.
- Open Spotify track, album, playlist and artist links or URIs.
- Play individual tracks.
- Queue the rest of the current page from a selected track.
- Queue the rest of a playlist from a selected track.
- Keep local Kodi Favorites for songs, albums, artists and playlists, grouped by type.
- View current playback, configured audio quality and Spotify Connect pairing status.
- Identify followed and shared playlists and get guidance when Spotify blocks playlist contents.
- Open supported artist views from track, album and artist context menus.
- Cache successful catalogue responses.
- Refresh playlist membership in the background while signed in.

The catalogue uses Spotify's regular Web API only. There is no public catalogue
fallback service.

<details>
<summary><b>Setup Details</b></summary>

### One Setup, Two Spotify Authorizations

Resonance uses two Spotify sign-ins because playback and browsing need different
tokens. The add-on presents them as one setup flow.

| Purpose | Method | Used for |
| --- | --- | --- |
| Playback | Spotify Device Connect | Pairing the Kodi box as a Spotify playback device |
| Catalogue | Browser sign-in with PKCE | Search, playlists, albums, artists, links and track metadata |

### Pair Playback

1. Open **Resonance**.
2. Choose **Set up Spotify**.
3. On your phone or computer, open the Spotify app with your Premium account.
4. Open the device picker and select the `Kodi-Resonance-XXXX` device.
5. Wait for the playback-pairing notification. This is step 1; browser sign-in is still required for search and playlists.

Your phone or computer and the Kodi box must be on the same network.

### Create A Spotify App

The catalogue sign-in needs your own Spotify Web API Client ID. Resonance does
not bundle a shared Client ID.

1. Open the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard).
2. Create an app with Web API access.
3. Add this Redirect URI exactly:

```text
https://resonance-spotify-callback.vercel.app/api/callback
```

4. Copy the app's 32-character Client ID.
5. In Kodi, open **Resonance -> Settings -> Advanced**.
6. Paste the Client ID into **Spotify Web API client ID**.
7. Leave **Web Auth redirect URI** set to the hosted callback URL above unless you run your own callback relay.

### Sign In For Catalogue Browsing

1. Open **Resonance -> Set up Spotify** or **Settings -> Account & Connection -> Set up Spotify**.
2. After playback pairing, scan the QR code shown by Kodi or use the URL below it on another device.
3. Approve the Spotify request.
4. The hosted callback relay stores the authorization code briefly, and Kodi polls it automatically.
5. The QR panel closes automatically when sign-in completes. Search, playlists and Spotify links then use the browser Web API token.

If the dialog times out after approval, use **Resume browser sign-in (paste
code)** and paste the `code` value from the browser address bar.

</details>

<details>
<summary><b>Usage And Settings</b></summary>

### Usage

- Open **Playlists** to browse account playlists.
- Open **Search** and choose tracks, albums, artists or playlists.
- Add a song, album, artist or playlist to **Favorites** from its context menu. Open **Favorites** to browse the Albums, Artists, Playlists and Songs sections.
- Select a track to play it.
- Use a track context menu to start playback from that row through the rest of the current page.
- From a playlist track, choose **Play playlist from here** to queue that song and the remaining playlist tracks.
- Use **Open Spotify link** for `open.spotify.com/...` URLs or `spotify:...` URIs.
- Use **Playback status** to see the current Kodi track, configured Spotify bitrate and playback pairing.
- Use **Account and status** to refresh playlists, re-pair playback or run setup.

Playlist data refreshes roughly every 30 minutes while signed in. A manual
refresh is available from **Account and status**.

### Local Favorites

Favorites are Resonance bookmarks stored in the add-on profile as
`favorites.json`. They are organized into **Albums**, **Artists**, **Playlists**
and **Songs**. Adding or removing a Favorite only changes this local file; it
does not make a Spotify API write, create a Spotify playlist, or save an item to
Spotify's Library. **Clear cache** leaves Favorites intact.

The file stores Spotify IDs and a snapshot of item metadata, not audio. Favorites
do not sync to other Kodi devices and are lost if the add-on profile is deleted
or Kodi's add-on data is cleared. Browsing a saved reference or playing a song
still needs a working Spotify sign-in, network access and an item that remains
available to the account. A local Favorite for a playlist does not grant access
to its tracks.

### Settings

| Category | Setting | Purpose |
| --- | --- | --- |
| Account & Connection | Set up Spotify | Runs the setup flow: Device Connect pairing for playback, then QR/browser PKCE sign-in for catalogue browsing. |
| Playback | Playback quality | Spotify bitrate: 96, 160 or 320 kbps. |
| Playback | Volume normalization | Spotty normalization mode: Track, Album or Automatic. |
| Maintenance | Sign out of Spotify | Clears the local sign-in state and catalogue cache. |
| Maintenance | Clear cache | Clears cached catalogue responses without removing sign-in state. |
| Advanced | Spotify Web API client ID | Your Spotify app Client ID for browser sign-in. |
| Advanced | Web Auth redirect URI | OAuth redirect URI; defaults to the hosted Resonance callback relay. |
| Advanced | Bypass cached API responses | Forces fresh Spotify Web API requests while still honoring cooldowns. |
| Advanced | Verbose debug logging | Writes additional diagnostics to the Kodi log. |

</details>

<details>
<summary><b>Platforms And Limits</b></summary>

### Supported Platforms

| Platform | Status |
| --- | --- |
| LibreELEC / Raspberry Pi ARM or AArch64 | Supported and tested |
| Android ARMv7 / AArch64 | Experimental; helper binaries are bundled |
| Windows x64, macOS, Linux x86/x64 | Not supported in this release; matching playback helper binaries are not bundled |

### Limits

- Spotify Premium is required for playback.
- Favorites store local Spotify item references and metadata only. They do not cache audio, work offline, sync to Spotify or other Kodi devices, or grant additional account access. If an item is deleted, region-blocked or no longer readable by the account, its Favorite may no longer open.
- Resonance does not currently save items to your Spotify Library or edit Spotify playlists. Spotify's February 2026 API migration replaced the older item-specific library write endpoints with `PUT /me/library` and changed related scopes. Resonance avoids those write endpoints and their extra permissions; local Favorites remain Kodi-only. See Spotify's [February 2026 migration guide](https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide).
- Spotify only allows playlist-item reads for playlists you own or collaborate on. Saving a followed playlist as a local Kodi Favorite does not grant access to its songs or clone it. See Spotify's [Get Playlist Items reference](https://developer.spotify.com/documentation/web-api/reference/get-playlists-items).
- Lyrics, podcasts, audiobooks, recently played and top lists are not included.
- Spotify Web API availability, account permissions, app Development Mode restrictions and rate limits determine what catalogue pages can be loaded.
- Playback is streaming-only; offline downloads are not supported.

</details>

<details>
<summary><b>Troubleshooting</b></summary>

### Device Connect Does Not Complete

- Confirm the phone or computer and Kodi box are on the same network.
- Make sure Spotify Premium is active on the account in the Spotify app.
- Retry pairing from Resonance.
- Check `/storage/.kodi/temp/kodi.log` for `DEVICE_CONNECT_DIAG` messages.

### Browser Sign-In Fails

- Confirm **Spotify Web API client ID** is a complete 32-character Client ID.
- Confirm **Web Auth redirect URI** exactly matches the Redirect URI in the Spotify Developer Dashboard.
- Use the full URL written to:

```text
/storage/.kodi/userdata/addon_data/plugin.audio.resonance/web-auth-url.txt
```

- If approval succeeded but Kodi stopped waiting, use the paste-code resume option.

### Playlists Are Empty

- Complete browser sign-in; playlists use the browser Spotify Web API token.
- Refresh playlists from **Account and status**.
- Check that the Spotify account actually has playlists visible to the app.

### A Followed Playlist Will Not Open

Spotify can list followed playlists that the Web API will not let this app read
unless you own the playlist or are a collaborator. Resonance keeps those
playlists visible, but opening one shows a message telling you to make your own
copy in Spotify and refresh playlists.

### Tracks Do Not Play

- Confirm Device Connect pairing is complete.
- Confirm the account has Spotify Premium.
- Restart Kodi after installing or updating.
- Some tracks may be unavailable in your region or unavailable to the account.

</details>

<details>
<summary><b>Development</b></summary>

Run the tests from the repository root:

```bash
python3 -m unittest discover -s tests
```

Run a syntax pass:

```bash
find . -name '*.py' -print0 | xargs -0 python3 -m py_compile
```

Important paths:

| Path | Purpose |
| --- | --- |
| `plugin.py` | Kodi plugin/browser entry point |
| `service.py` | Background service for auth renewal, playlist refresh and audio streaming |
| `resources/settings.xml` | Add-on settings |
| `resources/lib/catalogue.py` | Spotify Web API catalogue, cache and cooldown handling |
| `resources/lib/web_auth.py` | Browser PKCE login and token refresh |
| `resources/lib/qr_dialog.py` | Local QR-code popup helper for login links |
| `resources/lib/deps/qrcodegen.py` | Bundled pure-Python QR-code generator |
| `resources/lib/spotty*.py` | Spotty/librespot helper selection and audio transport |
| `tests/` | Unit tests |

</details>

<details>
<summary><b>Credits, License And Disclaimer</b></summary>

Resonance includes work made possible by the Kodi Spotify add-on community and
the bundled open-source dependencies.

Thanks and credit to:

- Marcel Veldt, Ldsz, Elkropac, FernetMenta and earlier Kodi Spotify add-on contributors.
- [`glk1001/plugin.audio.spotify`](https://github.com/glk1001/plugin.audio.spotify).
- [`herberto08/plugin.audio.spotify2-releases`](https://github.com/herberto08/plugin.audio.spotify2-releases).
- [Michael Herger](https://github.com/michaelherger) and the [`librespot-org/librespot`](https://github.com/librespot-org/librespot) community.
- The [`spotipy`](https://github.com/spotipy-dev/spotipy) project.
- The [`bottle`](https://github.com/bottlepy/bottle) project.
- Project Nayuki's [`QR Code generator library`](https://www.nayuki.io/page/qr-code-generator-library), bundled under the MIT License.

This project is distributed under the terms in [`LICENSE.txt`](LICENSE.txt).
Bundled dependencies and native helper binaries may carry their own upstream
licensing and attribution requirements.

Resonance is provided as-is, without warranty or guarantee of functionality.
Spotify is a trademark of Spotify AB. This add-on uses Spotify-related APIs and
playback components but is not endorsed, sponsored, certified or otherwise
approved by Spotify.

</details>