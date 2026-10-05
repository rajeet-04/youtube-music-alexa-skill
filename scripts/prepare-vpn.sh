#!/usr/bin/env bash
# Gluetun's custom WireGuard mode needs the peer Endpoint as an IP address, but Surfshark's
# files use a hostname. This resolves it and writes vpn/.generated/wg0.conf (mode 600,
# git-ignored). Run it before `docker compose ... up`, and again to refresh the IP.
#
#   scripts/prepare-vpn.sh [profile]     # default: VPN_PROFILE from .env, else in-mum
# Never prints keys.
set -euo pipefail
cd "$(dirname "$0")/.."
profile="${1:-${VPN_PROFILE:-}}"
if [ -z "$profile" ] && [ -f .env ]; then
    profile=$(grep -E '^VPN_PROFILE=' .env | tail -1 | cut -d= -f2- | tr -d '"' || true)
fi
profile="${profile:-in-mum}"
src="vpn/wireguard/${profile}.conf"
[ -f "$src" ] || { echo "prepare-vpn: $src not found" >&2; exit 2; }
endpoint=$(sed -nE 's/^[[:space:]]*Endpoint[[:space:]]*=[[:space:]]*([^[:space:]]+).*/\1/p' "$src" | head -1)
[ -n "$endpoint" ] || { echo "prepare-vpn: no Endpoint in $src" >&2; exit 2; }
host="${endpoint%:*}"
port="${endpoint##*:}"
if [[ "$host" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    ip="$host"
else
    ip=$(getent ahostsv4 "$host" | awk 'NR==1{print $1}')
fi
[ -n "$ip" ] || { echo "prepare-vpn: could not resolve $host" >&2; exit 75; }
mkdir -p vpn/.generated
umask 077
sed -E "s#^([[:space:]]*Endpoint[[:space:]]*=[[:space:]]*).*#\1${ip}:${port}#" "$src" > vpn/.generated/wg0.conf
chmod 600 vpn/.generated/wg0.conf
echo "prepare-vpn: profile=$profile endpoint=$host -> $ip:$port (wrote vpn/.generated/wg0.conf)"
