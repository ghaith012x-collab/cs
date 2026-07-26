#!/bin/bash
set -e

echo "Starting CLIP Vision AI Captcha Solver..."
echo "Email: alistra742@gmail.com"

# Start TOR for IP rotation (optional)
echo "Starting TOR..."
tor -f /etc/tor/torrc 2>/dev/null &
TOR_PID=$!
sleep 2
if kill -0 $TOR_PID 2>/dev/null; then
    echo "TOR ready (SOCKS5 :9050)"
else
    echo "TOR not available"
fi

# Start the Python app
echo "Starting web server + Discord automation..."
exec python -u app.py
