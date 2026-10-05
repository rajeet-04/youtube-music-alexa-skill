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
# Gluetun logs "Public IP address is <ip> (<country>, <region>, <city> ...)" after connecting.
line=$(docker logs "$CONTAINER" 2>&1 | grep "Public IP address is" | tail -1 || true)
echo "gluetun reports: ${line#*Public IP address is }"
case "${EXPECTED_COUNTRY:-IN}" in
    IN) want="India" ;;
    *) want="${EXPECTED_COUNTRY}" ;;
esac
if [ -z "$line" ] || ! grep -q "($want" <<<"$line"; then
    echo "verify-vpn: exit is not ${want}; check the profile in ./vpn (or wait for the VPN to connect)" >&2
    exit 1
fi
echo "OK: exit is in ${want}."
echo "Kill-switch: with the VPN down, 'docker exec $CONTAINER wget -T 5 -qO- https://ifconfig.co' must FAIL (do not test on a live service)."
