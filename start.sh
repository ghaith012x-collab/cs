#!/bin/bash
set -e

echo "Starting Eyes GEN — Discord token generator"
echo "========================================"

# ── Mullvad VPN daemon ────────────────────────────────────
if [ -x /usr/bin/mullvad-daemon ]; then
    echo "[Mullvad] Starting daemon..."
    /usr/bin/mullvad-daemon -v --disable-stdout-timestamps 2>&1 | \
        sed 's/^/[mullvad-d] /' &
    sleep 3

    if [ -n "$MULLVAD_LOGIN" ]; then
        echo "[Mullvad] Logging in..."
        mullvad account login "$MULLVAD_LOGIN" 2>&1 || echo "[Mullvad] Login failed"
        sleep 1
        echo "[Mullvad] Connecting..."
        mullvad connect 2>&1 || echo "[Mullvad] Connect failed (may need --cap-add=NET_ADMIN --device=/dev/net/tun)"
    else
        echo "[Mullvad] MULLVAD_LOGIN not set — skipping auto-connect"
    fi
else
    echo "[Mullvad] CLI not installed — VPN rotation disabled"
fi

# Start TOR for IP rotation (fallback)
echo "[TOR] Starting..."
tor -f /etc/tor/torrc 2>/dev/null &
sleep 2
echo "[TOR] Ready (SOCKS5 :9050)" 2>/dev/null || echo "[TOR] Not available"

# Start the Python app
echo ""
echo "Starting web server..."
exec python -u app.py
