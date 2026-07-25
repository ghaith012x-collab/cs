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
        echo "ERROR: Ollama failed to start within 60s"
        exit 1
    fi
    sleep 1
done

# Ensure the model is available (pull if missing)
MODEL="${OLLAMA_MODEL:-qwen2.5vl:3b}"
echo "Checking model: $MODEL"
if ! ollama list 2>/dev/null | grep -q "$MODEL"; then
    echo "Pulling $MODEL..."
    if ! ollama pull "$MODEL" 2>&1; then
        echo "ERROR: Failed to pull model $MODEL"
        echo "Try one of: qwen2.5vl:3b, qwen2.5vl:7b"
        exit 1
    fi
    echo "Model $MODEL pulled successfully"
fi

# Start the Python app
echo "Starting Discord Automation..."
exec python -u app.py
