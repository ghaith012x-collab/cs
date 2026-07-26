#!/bin/bash
set -e

echo "Starting Discord Automation with NopeCHA extension + API solvers..."

# Start TOR for IP rotation (optional)
echo "Starting TOR..."
tor -f /etc/tor/torrc 2>/dev/null &
TOR_PID=$!
sleep 2
if kill -0 $TOR_PID 2>/dev/null; then
    echo "TOR is ready (SOCKS5 :9050)"
else
    echo "TOR not available — running without proxy"
fi

# Start the Python app
echo "Starting web server..."
exec python -u app.py
