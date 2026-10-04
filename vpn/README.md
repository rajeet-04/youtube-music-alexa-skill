# VPN profile placeholders (Surfshark / India)

Real VPN files are **never committed** (`.gitignore` blocks them). Drop yours here:

| Mode | File | Also set in `.env` |
|---|---|---|
| WireGuard (default) | `vpn/wireguard/wg0.conf` | `VPN_TYPE=wireguard` |
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
