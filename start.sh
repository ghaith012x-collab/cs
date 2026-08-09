#!/bin/bash
set -e

echo "Starting NoCaptchaAI Captcha Solver..."
echo "Email: auto-generated via duckmail.sbs (or config.json)"
echo "NoCaptchaAI API key: $([ -n "$API_KEY" ] && echo 'set' || echo 'NOT SET - FunCAPTCHA offline solver only')"

# AI text model — qwen3:1.7b (fast + knows captcha trivia). Override via env.
export OLLAMA_MODEL="${OLLAMA_MODEL:-qwen3:1.7b}"
export OLLAMA_TEXT_MODEL="${OLLAMA_TEXT_MODEL:-qwen3:1.7b}"
export OLLAMA_VOTES="${OLLAMA_VOTES:-1}"  # 1 vote = fastest; 2+ for majority on fast servers

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
