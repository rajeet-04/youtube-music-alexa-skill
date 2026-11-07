# Running JUKES on a small cloud VM (Azure) behind a Cloudflare named tunnel

Tested on Azure `Standard_B2als_v2` (2 vCPU, 4 GB), Ubuntu 22.04, Docker. The same steps work on
any VM. Do not use 1 GB: the services use about 740 MiB idle and Chromium needs about 1 GB more.

## Compose files

    docker-compose.yml               base: ytmusic, youtube-browser, caddy
    docker-compose.tunnel-named.yml  cloudflared (needs CLOUDFLARE_TUNNEL_TOKEN in .env)
    docker-compose.fast.yml          warm ytresolver sidecar (same network as gluetun) + fork server + progressive audio
    docker-compose.novpn.yml         no VPN: a stand-in "gluetun" container holds the shared network
                                     namespace (use docker-compose.vpn.yml instead for a real VPN)

Start (no VPN):

    docker network create --subnet 172.18.0.0/16 web
    F="-f docker-compose.yml -f docker-compose.tunnel-named.yml -f docker-compose.novpn.yml -f docker-compose.fast.yml"
    docker compose $F up -d

Swap `docker-compose.novpn.yml` for `docker-compose.vpn.yml` to route everything through Gluetun.
The app waits for Gluetun to be healthy, so a broken VPN config takes the service down; test a
profile in a throwaway Gluetun container first.

## Fixes in this branch

- `browser-auth/entrypoint.sh`: `x11vnc -noipv6 -threads`. Without them x11vnc spun at 100% CPU and
  never answered the RFB handshake, so noVNC stayed on "connecting".
- `flask-server/jukes/music.py`: searches always use the anonymous client, so they are not written to
  the signed-in account's YouTube Music search history. Radio still uses the personalised client.
- `scripts/jukes-crashlog.sh` + `.service` (optional): saves the container log tail, exit state and
  `dmesg` to `/var/log/jukes-crash/` whenever a container exits non-zero. Capped at 150 MB.

## Secrets

`.env`, `vpn/wireguard/*.conf` and `vpn/.generated/` are git-ignored. Set the tunnel token and the
admin password hash with hidden prompts; never paste them into chat or commit them.

## Sizing notes

- 1 GB VM: load average 19 and constant 502s while Chromium ran. 4 GB: load under 1.
- Stop `youtube-browser` when nobody is signing in to save about 1 GB.

## VPN exit near the VM

The VM is in Azure Malaysia West. A London exit costs about 242 ms per TCP connect and about 5 s per
cold song; a Singapore exit costs about 8 ms and under 1 s. Gluetun's built-in Surfshark list picks
the server (the per-server WireGuard public keys differ, so copying a profile and changing only the
Endpoint does not work). In `.env`:

    VPN_SERVICE_PROVIDER=surfshark  VPN_TYPE=wireguard
    WIREGUARD_PRIVATE_KEY=<from your Surfshark wg conf>  WIREGUARD_ADDRESSES=10.14.0.2/16
    VPN_NATIVE_SERVER_COUNTRIES=Singapore

Move `vpn/.generated/wg0.conf` aside: gluetun reads it automatically and its endpoint port conflicts
with the built-in server list ("endpoint port is set"). Roll back by restoring the old `.env` values
(`VPN_SERVICE_PROVIDER=custom`) and moving the file back. After any gluetun recreate, run
`docker restart ytresolver` if it shows unhealthy: it may have started during gluetun's restart loop.
