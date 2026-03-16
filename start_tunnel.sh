#!/bin/bash
# Start Hivemind + Cloudflare Tunnel
# Usage: ./start_tunnel.sh

set -e

HIVEMIND_PORT=8080

echo "Starting Hivemind tunnel..."
echo "This will give you a public HTTPS URL accessible from anywhere (phone, etc.)"
echo ""

# Start cloudflare quick tunnel (no account needed)
cloudflared tunnel --url http://localhost:$HIVEMIND_PORT 2>&1 | tee /tmp/hivemind_tunnel.log &
TUNNEL_PID=$!

echo "Tunnel PID: $TUNNEL_PID"
echo "Waiting for URL..."
sleep 4

# Extract the URL from logs
URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' /tmp/hivemind_tunnel.log | head -1)

if [ -n "$URL" ]; then
    echo ""
    echo "Hivemind is accessible at:"
    echo "   $URL"
    echo ""
    echo "Open this on your phone: $URL"
    # Copy to clipboard if possible
    echo "$URL" | pbcopy 2>/dev/null && echo "URL copied to clipboard"
else
    echo "URL not yet available, check /tmp/hivemind_tunnel.log"
fi

echo ""
echo "Press Ctrl+C to stop the tunnel"
wait $TUNNEL_PID
