# VPN profile placeholders (Surfshark / India)

Real VPN files are **never committed** (`.gitignore` blocks them). Drop yours here:

| Mode | File | Also set in `.env` |
|---|---|---|
| WireGuard (default) | `vpn/wireguard/<name>.conf`, e.g. `in-mum.conf`, `in-del.conf` | `VPN_TYPE=wireguard`, `VPN_PROFILE=<name>` |
| OpenVPN | `vpn/openvpn/surfshark.ovpn` | `VPN_TYPE=openvpn`, `OPENVPN_CUSTOM_CONFIG=/gluetun/openvpn/surfshark.ovpn`, `OPENVPN_USER`, `OPENVPN_PASSWORD` |

Templates: `vpn/wireguard/wg0.conf.example`, `vpn/openvpn/surfshark.ovpn.example`.

Pick an **India** server when you generate the config in the Surfshark dashboard
(Manual setup → WireGuard or OpenVPN → location India). Whatever endpoint is in
the file is the exit country; `VPN_SERVER_COUNTRIES` only applies if you switch to
Gluetun's built-in `surfshark` provider instead of a custom file.

Both directories are bind-mounted read-only into Gluetun, so they must exist
(they do: each holds a tracked `.example`/`.gitkeep`).

After starting, verify (do not trust the config alone):

    scripts/verify-vpn.sh      # exit country, DNS, kill-switch note

Do not paste VPN keys, passwords or config contents into chat, issues or logs.

Surfshark WireGuard files for several locations can sit side by side; `VPN_PROFILE`
selects which one is mounted as Gluetun's `wg0.conf`. Keep them `chmod 600`. Only
use India (`in-*`) profiles for this service; switching `VPN_PROFILE` and recreating
the `gluetun` container changes the exit.

## Required before first start

```bash
scripts/prepare-vpn.sh      # resolves the Endpoint hostname to an IP (Gluetun requires an IP)
```

Re-run it after changing `VPN_PROFILE` or if the connection stops working (server IPs can change),
then `docker compose ... up -d --force-recreate gluetun`.
