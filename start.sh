#!/bin/bash
set -e

# Start Ollama in background
ollama serve &
OLLAMA_PID=$!

# Wait for Ollama to be ready (up to 60s)
echo "Starting Ollama..."
for i in $(seq 1 60); do
    if curl -s http://localhost:11434/api/tags > /dev/null 2>&1; then
        echo "Ollama is ready!"
        break
    fi
    if [ "$i" -eq 60 ]; then
        echo "Ollama failed to start within 60s"
        exit 1
    fi
    sleep 1
done

# Ensure the model is available (pull if missing)
MODEL="${OLLAMA_MODEL:-qwen2.5-vl:3b-instruct-q4_K_M}"
echo "Checking model: $MODEL"
ollama list 2>/dev/null | grep -q "$MODEL" || {
    echo "Pulling $MODEL (first run only)..."
    ollama pull "$MODEL"
}

# Start the Python app
echo "Starting Discord Automation..."
exec python -u app.py
