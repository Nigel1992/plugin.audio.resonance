# Resonance Spotify callback relay (Vercel)

Receives the Spotify authorization-code redirect for Resonance's browser
login when the callback cannot reach the LibreELEC box directly (no public
IP, and a phone/PC cannot reach the box's `127.0.0.1:53209`).

Flow when using this relay:

1. The add-on sends the user to Spotify with `redirect_uri` = your Vercel app,
   e.g. `https://<your-app>.vercel.app/api/callback`.
2. Spotify redirects the user's browser to that URL with `?code=...&state=...`.
3. `/api/callback` stores `code` for `state` in a KV store for 10 minutes and
   shows "Resonance: Spotify connected".
4. The Kodi box polls `/api/poll?state=...` until the code arrives, then
   exchanges it (still with the Vercel `redirect_uri`, as Spotify requires).

## Deploy

### Deploy from your own GitHub fork

1. Fork the [Resonance repository](https://github.com/Nigel1992/plugin.audio.resonance)
   to your GitHub account and review this callback folder's source.
2. In Vercel, select **Add New → Project**, import your fork, and set the
   project's **Root Directory** to `vercel/resonance-callback`. Deploy the
   project with its included `api/`, `package.json`, and `vercel.json` files.
3. Add an Upstash Redis database to the Vercel project using the
   [Vercel Marketplace integration](https://vercel.com/marketplace/upstash).
   The handler accepts either `KV_REST_API_URL` + `KV_REST_API_TOKEN` or
   `UPSTASH_REDIS_REST_URL` + `UPSTASH_REDIS_REST_TOKEN`. Redeploy after the
   integration adds its environment variables. A provider plan may have usage
   charges.
4. Check `https://<your-app>.vercel.app/api/status`; it should return
   `{"ok":true,"service":"resonance-callback"}`. With Redis connected,
   `/api/poll?state=manual-check` should return `{"waiting":true}`.

Vercel supports importing a Git repository and setting its project root in the
[deployment setup](https://vercel.com/docs/git). For command-line deployment,
run `vercel` or `vercel --prod` from this folder after linking the project and
setting the same Redis variables.

## One-off Spotify Dashboard step

Add `https://<your-app>.vercel.app/api/callback` to **Redirect URIs** in your
Spotify Developer Dashboard app (Settings → the app you use for the Client ID).
It must match the redirect URL used by Resonance exactly; Spotify documents
these [redirect URI requirements](https://developer.spotify.com/documentation/web-api/concepts/redirect_uri).

## Then in Resonance (Kodi)

Set **Settings → Advanced → Web Auth redirect URI** to
`https://<your-app>.vercel.app/api/callback` and run **Sign in to Spotify with
a browser**. The preview page also shows the code, so the in-Kodi **Paste
code** fallback works if polling is unavailable.

## What the relay handles

The callback stores only the short-lived authorization code in Redis, keyed by
the OAuth `state`, with a 10-minute expiry. Kodi polls `/api/poll`; the relay
deletes the code when it returns it. Kodi exchanges the code using its local
PKCE verifier. The relay does not need the Spotify Client Secret or the
resulting access/refresh tokens.

Redis is optional only if you use the manual **Paste code** fallback. Without
the Redis URL and token, the callback page displays the code but `/api/poll`
returns `503 storage not configured`. Never commit Redis credentials into the
repository.
