#!/usr/bin/env bash
# Report the VPN exit country/IP as seen from inside the shared network namespace.
# Does not print credentials. Usage: scripts/verify-vpn.sh [container]
set -euo pipefail
CONTAINER="${1:-${VPN_CONTAINER_NAME:-gluetun}}"
if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
    echo "verify-vpn: container '$CONTAINER' not found" >&2
    exit 75
fi
health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$CONTAINER")
echo "gluetun health: $health"
country=$(docker exec "$CONTAINER" wget -qO- -T 10 https://ipinfo.io/country 2>/dev/null || true)
echo "exit country: ${country:-unknown}  (expected: IN)"
if [ "${country:-}" != "IN" ]; then
    echo "verify-vpn: exit is not India; check the config in ./vpn" >&2
    exit 1
fi
echo "Kill-switch: with the VPN down, 'docker exec $CONTAINER wget -T 5 -qO- https://ipinfo.io' must FAIL (do not test on a live service)."
