# Cloudflare placeholders

Nothing here is deployed automatically. Fill in once you have the hostname.

- **DNS**: proxied record for `SITE_ADDRESS` pointing at this host (or use a
  Cloudflare Tunnel to `http://localhost:80`; if you do, verify the trusted-proxy
  settings in `SETUP-DOCKER.md` before enabling any forwarded-header trust).
- **Cache rules**: bypass cache for `/audio/*`, `/v1/*`, `/admin/*`,
  `/youtube-login/*`. Audio is intentionally not edge-cached so origin LRU
  accounting stays accurate.
- **Rate limiting / WAF** (optional): the app already limits per caller; edge
  limits are an extra layer. Do not challenge `/audio/*` or `/v1/*`: the Android
  app cannot solve browser challenges.
- **Wrangler**: `bun x wrangler login`, then manage rules/Workers as needed. No
  `wrangler.toml` is required by the backend.
