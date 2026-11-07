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
    # The hostname rotates over many servers and only some accept our key, so the first DNS answer
    # is often dead. Prefer an address that answers a real WireGuard handshake (fastest first).
    # Skipped silently when Docker or the image is unavailable. Re-running while Gluetun is up
    # skips its current server, so the live tunnel is never poked.
    image="${JUKES_IMAGE:-youtube-music-alexa-skill-ytmusic}"
    if [ "${PREPARE_VPN_PROBE:-1}" = 1 ] && command -v docker >/dev/null 2>&1 \
        && docker image inspect "$image" >/dev/null 2>&1; then
        skip=()
        if [ -f vpn/.generated/wg0.conf ] && docker ps --format '{{.Names}}' | grep -qx "${VPN_CONTAINER_NAME:-gluetun}"; then
            live=$(sed -nE 's/^[[:space:]]*Endpoint[[:space:]]*=[[:space:]]*([0-9.]+):.*/\1/p' vpn/.generated/wg0.conf | head -1)
            [ -n "$live" ] && skip=(--skip "$live")
        fi
        if best=$(docker run --rm --network host --entrypoint python \
            -v "$PWD/scripts/wg_probe.py:/wg_probe.py:ro" -v "$PWD/$src:/wg.conf:ro" \
            "$image" /wg_probe.py /wg.conf "${skip[@]}" 2>&1 >/dev/stdout | tee /dev/stderr | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | head -1) && [ -n "$best" ]; then
            ip="$best"
        else
            echo "prepare-vpn: no address answered the handshake probe; using first DNS answer" >&2
        fi
    fi
fi
[ -n "$ip" ] || { echo "prepare-vpn: could not resolve $host" >&2; exit 75; }
mkdir -p vpn/.generated
umask 077
sed -E "s#^([[:space:]]*Endpoint[[:space:]]*=[[:space:]]*).*#\1${ip}:${port}#" "$src" > vpn/.generated/wg0.conf
chmod 600 vpn/.generated/wg0.conf
echo "prepare-vpn: profile=$profile endpoint=$host -> $ip:$port (wrote vpn/.generated/wg0.conf)"
