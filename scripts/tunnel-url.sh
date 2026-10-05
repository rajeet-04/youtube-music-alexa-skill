#!/usr/bin/env bash
# Print the current Cloudflare Quick Tunnel URL (changes whenever cloudflared is recreated).
set -euo pipefail
for _ in $(seq 1 30); do
    url=$(docker logs "${1:-cloudflared}" 2>&1 | grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1 || true)
    if [ -n "$url" ]; then echo "$url"; exit 0; fi
    sleep 2
done
echo "tunnel-url: no URL yet; check 'docker logs cloudflared'" >&2
exit 75
