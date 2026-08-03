#!/bin/bash
set -e

echo "Starting NoCaptchaAI Captcha Solver..."
echo "Email: auto-generated via duckmail.sbs (or config.json)"
echo "NoCaptchaAI API key: $([ -n "$API_KEY" ] && echo 'set' || echo 'NOT SET - FunCAPTCHA offline solver only')"

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
