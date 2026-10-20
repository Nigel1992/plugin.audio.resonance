import { Redis } from "@upstash/redis";

const REST_URL =
  process.env.KV_REST_API_URL ||
  process.env.UPSTASH_REDIS_REST_URL ||
  process.env.REDIS_REST_URL ||
  "";
const REST_TOKEN =
  process.env.KV_REST_API_TOKEN ||
  process.env.UPSTASH_REDIS_REST_TOKEN ||
  process.env.REDIS_REST_TOKEN ||
  "";

const redis = REST_URL && REST_TOKEN ? new Redis({ url: REST_URL, token: REST_TOKEN }) : null;

const TTL_SECONDS = 600;

const HTML_OK = `<!doctype html><html><head><meta charset="utf-8"><title>Resonance</title>
<style>body{background:#121212;color:#1DB954;font-family:Arial,sans-serif;text-align:center;padding-top:80px}h1{font-size:24px}p{color:#b3b3b3}</style></head>
<body><h1>Resonance: Spotify connected.</h1><p>You can close this tab.</p></body></html>`;

function pathOf(req, url) {
  const parsed = new URL(url, "https://resonance.example");
  return parsed.pathname;
}

function query(url) {
  const parsed = new URL(url, "https://resonance.example");
  return parsed.searchParams;
}

function json(res, status, payload) {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.end(JSON.stringify(payload));
}

// Callback handler: Spotify redirects the user's browser here after approval.
async function handleCallback(res, url) {
  const params = query(url);
  const code = params.get("code");
  const state = params.get("state");
  const error = params.get("error");

  if (error) {
    res.statusCode = 400;
    res.setHeader("Content-Type", "text/html; charset=utf-8");
    res.end(`<html><body style="background:#121212;color:#e22134;font-family:Arial;text-align:center;padding-top:80px"><h1>Spotify login was refused: ${error}</h1></body></html>`);
    return;
  }
  if (!code || !state) {
    json(res, 400, { error: "missing code or state" });
    return;
  }
  if (redis) {
    await redis.set(`resonance:web-auth:${state}`, code, { ex: TTL_SECONDS });
  }
  res.statusCode = 200;
  res.setHeader("Content-Type", "text/html; charset=utf-8");
  res.end(
    HTML_OK +
      `<p>Code stored for this box.</p><p style="word-break:break-all;max-width:70%;margin:1em auto">` +
      code +
      `</p>`
  );
}

// Poll handler: the Kodi box asks for the code that arrived for `state`.
async function handlePoll(res, url) {
  const params = query(url);
  const state = params.get("state");
  if (!state) {
    json(res, 400, { error: "missing state" });
    return;
  }
  if (!redis) {
    json(res, 503, { error: "storage not configured" });
    return;
  }
  const stored = await redis.get(`resonance:web-auth:${state}`);
  if (!stored) {
    json(res, 200, { waiting: true });
    return;
  }
  const code = String(stored);
  await redis.del(`resonance:web-auth:${state}`);
  json(res, 200, { code });
}

export default async function handler(req, res) {
  const base = `http://${req.headers.host || "resonance.example"}`;
  const url = req.url || "/";
  const path = pathOf(req, url);
  try {
    if (path === "/api/callback") {
      return await handleCallback(res, url);
    }
    if (path === "/api/poll") {
      return await handlePoll(res, url);
    }
    json(res, 200, { ok: true, service: "resonance-callback" });
  } catch (err) {
    console.error("resonance-callback error:", err);
    json(res, 500, { error: "internal error" });
  }
}