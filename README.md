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
| Version | 1.1.0 |

| | |
| --- | --- |
| [![License](https://img.shields.io/github/license/Nigel1992/plugin.audio.resonance)](LICENSE.txt) | [![Release](https://img.shields.io/github/v/release/Nigel1992/plugin.audio.resonance?sort=semver)](https://github.com/Nigel1992/plugin.audio.resonance/releases) |
| [![Stars](https://img.shields.io/github/stars/Nigel1992/plugin.audio.resonance?style=social&label=Star)](https://github.com/Nigel1992/plugin.audio.resonance) | [![Python](https://img.shields.io/badge/language-Python%203-3776AB.svg)](https://www.python.org/) |

## Quick Start

1. Download `plugin.audio.resonance-1.1.0.zip` from the [Releases page](https://github.com/Nigel1992/plugin.audio.resonance/releases).
2. In Kodi, install it with **Add-ons -> Install from zip file**.
3. Restart Kodi once.
4. Open **Add-ons -> Music add-ons -> Resonance**.
5. Create a Spotify Developer app, add the redirect URI below, and paste its Client ID into **Settings -> Advanced**.
6. Use **Set up Spotify**. Resonance pairs playback first, then shows a QR code and URL for browser sign-in.

### Upgrading from 1.0.x

1. Install the new zip over the old version and restart Kodi.
2. Open **Account and status** and choose **Reauthorize Spotify** once. Spotify asks for the library and playlist permissions that the new write actions need.
3. The local Favorites are gone. Use **Your Spotify Library** and the Spotify write actions instead: liked tracks, saved albums, followed artists and your playlists are read straight from Spotify.

```text
https://resonance-spotify-callback.vercel.app/api/callback
```

## Features

- Search Spotify tracks, albums and artists.
- Browse and play tracks from your Spotify Liked Songs library.
- Browse saved albums, followed artists, and saved or followed playlists in **Your Spotify Library**.
- Like or unlike tracks and save or remove albums in your Spotify library.
- Add tracks to playlists owned by your Spotify account.
- Create a playlist in your Spotify account, optionally with the track you started from.
- Unfollow a playlist or artist that you follow, so it leaves **Your Spotify Library**.
- Browse your Spotify playlists, including private playlists available to your account.
- Browse album tracks, playlist tracks and artist albums.
- Open Spotify track, album, playlist and artist links or URIs through the plugin's `open_link` action.
- Play individual tracks.
- Queue the rest of the current page from a selected track.
- Queue the rest of a playlist from a selected track.
- View current playback, configured audio quality and Spotify Connect pairing status.
- Read the account and status page at a glance: it is grouped into Account, Catalogue, Playback and Maintenance, with one icon per row.
- Give every menu row its own icon, bundled with the add-on, so the menus look the same in every skin.
- Identify followed and shared playlists and get guidance when Spotify blocks playlist contents.
- Open supported artist views from track, album and artist context menus: everything, top songs, albums, singles and appears on.
- See the release notes of the running version in a big window after every update, and any time under **Account and status** with the row **What's new**.
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
| Catalogue | Browser sign-in with PKCE | Search, playlists, Liked Songs, saved albums, followed artists, links and track metadata |

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

### Host Your Own Spotify Callback Relay on Vercel

The default callback is hosted by the Resonance project. If you prefer to
control the callback deployment and its temporary storage yourself, deploy the
included relay from your own GitHub and Vercel accounts:

1. Fork [the Resonance repository](https://github.com/Nigel1992/plugin.audio.resonance)
   to your GitHub account. Review the source in
   [`vercel/resonance-callback`](https://github.com/Nigel1992/plugin.audio.resonance/tree/main/vercel/resonance-callback)
   before deploying it.
2. In Vercel, choose **Add New → Project**, import your fork, and set **Root
   Directory** to `vercel/resonance-callback`. Keep the included `api/`,
   `package.json`, and `vercel.json` files. Deploy the project.
3. For automatic code delivery to Kodi, add an Upstash Redis database to the
   Vercel project using the [Vercel Marketplace integration](https://vercel.com/marketplace/upstash).
   The relay needs a REST URL and token. It recognizes
   `KV_REST_API_URL` with `KV_REST_API_TOKEN`, or
   `UPSTASH_REDIS_REST_URL` with `UPSTASH_REDIS_REST_TOKEN`. The integration
   normally adds these to the project environment. Redeploy after connecting
   the database. Choose a plan that fits your use; the provider may charge for
   usage.
4. Check `https://<your-project>.vercel.app/api/status`. It should return
   `{"ok":true,"service":"resonance-callback"}`. With Redis connected,
   `https://<your-project>.vercel.app/api/poll?state=manual-check` should return
   `{"waiting":true}`.
5. In the Spotify Developer Dashboard for the Client ID used by Resonance, add
   this exact HTTPS Redirect URI, replacing the project name:

   ```text
   https://<your-project>.vercel.app/api/callback
   ```

   Spotify requires the redirect URI in the authorization request to exactly
   match an entry in the app settings. See Spotify's
   [redirect URI requirements](https://developer.spotify.com/documentation/web-api/concepts/redirect_uri).
6. In Kodi, open **Resonance → Settings → Advanced** and set **Web Auth redirect
   URI** to that same `/api/callback` URL. Save it, then start browser sign-in
   again.

The relay temporarily stores the authorization code in Redis for up to 10
minutes, keyed by the one-time OAuth `state`; Kodi polls for it, and the relay
deletes it when delivered. The code is exchanged for tokens by Kodi using its
PKCE verifier. The relay does not need your Spotify Client Secret. Anyone
operating the deployed relay and its storage controls that part of the login
flow, which is why you may choose to run your own.

Redis is needed for automatic polling. Without Redis, the callback still shows
the code in the browser, so you can use Resonance's **Paste code** fallback;
automatic polling will report that storage is not configured. Keep the relay
and its Redis credentials private to your own Vercel project, and do not commit
database tokens into the fork.

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

- Open **Your Spotify Library** to browse Liked Songs, Saved Albums, Followed Artists, and Saved and Followed Playlists.
- Open **Search** and choose tracks, albums or artists.
- Use a track context menu to like/unlike it on Spotify or add it to one of your own playlists. Album context menus can save or remove the album from your Spotify library. These actions update your Spotify account.
- The context menu only offers the action that makes sense: **Like on Spotify** is hidden for tracks that are already in Liked Songs, and **Save album on Spotify** is hidden for albums that are already in your library. Both appear again when Spotify cannot answer the check.
- Open a playlist of your own and use the track context menu to remove a track from it. The entry is named **Remove from &lt;playlist&gt;** and always removes the track from that playlist, so there is no playlist to choose. The entry only shows inside a playlist the account owns, where the track really is in that playlist.
- Use a track context menu to unfollow a playlist from **Saved and Followed Playlists** and an artist from **Followed Artists**. Unfollowing only removes the item from your library; Spotify keeps the playlist or artist itself.
- Choose **Create playlist...** in **Your Spotify Library** to make a new playlist, or **Create playlist with this track...** in a track context menu to start one with that track. New playlists are created with the private flag, which Spotify only allows with the playlist modification permission that **Reauthorize Spotify** grants, and they show up in **Saved and Followed Playlists**.
- Select a track to play it.
- Use a track context menu to start playback from that row through the rest of the current page.
- From a playlist track, choose **Play playlist from here** to queue that song and the remaining playlist tracks.
- Spotify links are no longer on the main menu. Open `open.spotify.com/...` URLs or `spotify:...` URIs with the plugin's `open_link` action, for example from a Kodi favourites entry pointing at `plugin://plugin.audio.resonance/?action=open_link`.
- Use **Playback status** to see the current Kodi track, configured Spotify bitrate and playback pairing.
- Use **Account and status** to see the state of the add-on at a glance. The page is grouped into **Account** (sign-in state, reauthorize), **Catalogue** (playlists, token), **Playback** (status, pairing, quality) and **Maintenance** (clear cache, sign out). Every row has its own icon, so the page can be scanned without reading long sentences.
- Existing users need to choose **Reauthorize Spotify** under **Account and status** and approve the Spotify library, playlist, and followed-artist permissions before using these views or Spotify write actions.

Playlist data refreshes roughly every 30 minutes while signed in. A manual
refresh is available from **Account and status**.

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
- Spotify write actions use the current library and playlist endpoints. Like/unlike, save/remove album and unfollow playlist or artist use `PUT/DELETE /me/library`; adding a track uses `POST /playlists/{id}/items`, removing one uses `DELETE /playlists/{id}/items` and creating a playlist uses `POST /me/playlists`. The state of the library is read with `GET /me/library/contains`, so the context menu can hide actions that would not change anything. The account must authorize the corresponding library and playlist modification scopes. See Spotify's [February 2026 migration guide](https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide).
- Spotify only allows playlist-item reads for playlists you own or collaborate on. See Spotify's [Get Playlist Items reference](https://developer.spotify.com/documentation/web-api/reference/get-playlists-items).
- Spotify has no endpoint that lists the playlists a track belongs to, so a track can only be removed from a playlist from that playlist's own page. Resonance therefore offers **Remove from &lt;playlist&gt;** inside your playlists and nowhere else.
- There is no playlist search. Because Spotify never shares playlist contents with followers, a playlist search could only list playlists Resonance cannot open. Playlists stay available through **Your Spotify Library** and the `open_link` action.
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
- The pairing receiver writes its own log to:

```text
/storage/.kodi/userdata/addon_data/plugin.audio.resonance/runtime/device-connect.log
```

- When the receiver refuses to start, the tail of that file is copied into the Kodi log as `result=immediate_exit`.

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
| `resources/media/` | Menu icons bundled with the add-on |
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
