# Cloudflare

## No domain? Use a free Quick Tunnel (default)

```bash
docker compose -f docker-compose.yml -f docker-compose.vpn.yml -f docker-compose.tunnel.yml up -d --build
scripts/tunnel-url.sh        # -> https://<random>.trycloudflare.com
```

- No account, domain, open port or public IP needed; nothing is published on the host.
- Put that URL in the JUKES app's backend URL. **It changes whenever the `cloudflared`
  container is recreated** (not on a plain restart of the stack's other services), so
  re-run `scripts/tunnel-url.sh` after recreating it and update the app.
- Cloudflare offers quick tunnels for testing, with no uptime guarantee and a cap of
  200 concurrent in-flight requests. Fine for personal use; not for a public launch.
- Audio goes through Cloudflare's network. Heavy media delivery on free plans can be
  throttled under Cloudflare's terms; watch for that if usage grows.
- Admin and the YouTube sign-in window work through the same URL (`/admin/`).
- The overlay trusts only the internal tunnel peer, so rate limits use the real client IP
  and audio URLs come back as `https://<your-tunnel-host>/...`.

## Permanent address: named tunnel (e.g. ms.rajeet.in)

1. Put `rajeet.in` on Cloudflare (nameservers at the registrar; keep Vercel/GitHub records DNS-only).
2. Zero Trust > Networks > Tunnels > Create tunnel (Cloudflared). Copy the token.
3. In the tunnel's **Public Hostname** tab: `ms.rajeet.in` -> HTTP -> `caddy:80`.
4. On the server (the token is never printed or committed):
   ```bash
   read -rs -p 'Tunnel token: ' T; echo "CLOUDFLARE_TUNNEL_TOKEN=$T" >> .env; unset T
   docker compose -f docker-compose.yml -f docker-compose.vpn.yml -f docker-compose.tunnel-named.yml up -d --build
   curl -s https://ms.rajeet.in/health/live
   ```
`ms.rajeet.in` is proxied automatically and no DNS record exposes the server's IP. You do not
need to install `cloudflared` on the host: the container runs it.

## Want a stable address later? (generic notes)

Needs a domain on your Cloudflare account: create a **named tunnel** (`cloudflared tunnel
create`, route `api.yourdomain.com` to `http://caddy:80`, run it with a tunnel token instead
of `--url`), then add cache rules: bypass cache for `/audio/*`, `/v1/*`, `/admin/*`,
`/youtube-login/*`. Don't challenge `/audio/*` or `/v1/*` (the Android app can't solve
challenges).

Wrangler (`bun x wrangler login`) is not needed for any of this.
