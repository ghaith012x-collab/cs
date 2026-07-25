FROM python:3.11-slim

WORKDIR /app

# System dependencies + Ollama
RUN apt-get update && apt-get install -y \
    wget gnupg xvfb zstd libglib2.0-0 libnss3 libnspr4 libdbus-1-3 \
    libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
    libasound2 libcairo2 libpango-1.0-0 curl tor \
    && rm -rf /var/lib/apt/lists/*

# Install Ollama
RUN curl -fsSL https://ollama.com/install.sh | sh

# Install Python dependencies (ONNX Runtime for YOLO AI ~56ms CPU inference)
RUN pip install --no-cache-dir \
    playwright==1.40.0 \
    opencv-python-headless==4.9.0.80 \
    numpy==1.26.4 \
    aiofiles==23.1.0 \
    aiohttp==3.9.1 \
    Pillow==10.2.0 \
    onnxruntime>=1.18.0 \
    onnx>=1.16.0 \
    protobuf>=3.20.3

RUN python -m playwright install chromium

# Pre-pull Moondream model during build (~1GB download)
# Moondream is the smallest vision model on Ollama (1.6B params)
# Much faster on CPU than Qwen 3B
RUN ollama serve & \
    OLLAMA_PID=$! && \
    sleep 5 && \
    if ! ollama pull moondream 2>&1; then \
        echo "WARNING: Model pull failed, will retry at runtime"; \
    fi && \
    kill $OLLAMA_PID 2>/dev/null || true

# Pre-download YOLOv11n ONNX model (~5.5MB) for fast CPU inference
# This model runs at ~56ms per inference — replaces slow Moondream
RUN mkdir -p _models && \
    wget -q https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.onnx \
    -O _models/yolo11n.onnx && \
    echo "YOLO model downloaded" || echo "WARNING: YOLO download failed, will retry at runtime"

COPY app.py server.py captcha_solver.py vision_ai.py config.json requirements.txt ./
COPY torrc /etc/tor/torrc
COPY test/ ./test/

# Start script that runs Ollama + the Python app
COPY start.sh .
RUN chmod +x start.sh

EXPOSE 8080
ENV PYTHONUNBUFFERED=1
ENV PORT=8080
ENV OLLAMA_HOST=0.0.0.0
# Moondream — smallest vision model on Ollama (1.6B, ~1GB)
# Fast on CPU, designed for edge devices
ENV OLLAMA_MODEL=moondream

CMD ["./start.sh"]
