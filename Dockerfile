FROM python:3.11-slim

WORKDIR /app

# System dependencies + Ollama
RUN apt-get update && apt-get install -y \
    wget gnupg xvfb libglib2.0-0 libnss3 libnspr4 libdbus-1-3 \
    libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
    libasound2 libcairo2 libpango-1.0-0 curl \
    && rm -rf /var/lib/apt/lists/*

# Install Ollama
RUN curl -fsSL https://ollama.com/install.sh | sh

# Install Python dependencies (no more torch/torchvision/CLIP — saves ~2GB)
RUN pip install --no-cache-dir \
    playwright==1.40.0 \
    opencv-python-headless==4.9.0.80 \
    numpy==1.26.4 \
    aiofiles==23.1.0 \
    aiohttp==3.9.1 \
    Pillow==10.2.0

RUN python -m playwright install chromium

# Pre-pull the Qwen2.5-VL model during build (~1.8GB download)
# This makes it ready at runtime — no first-request delay
RUN ollama serve & \
    OLLAMA_PID=$! && \
    sleep 5 && \
    ollama pull qwen2.5-vl:3b-instruct-q4_K_M && \
    kill $OLLAMA_PID 2>/dev/null; \
    echo "Qwen2.5-VL 3B model ready"

COPY app.py server.py captcha_solver.py config.json requirements.txt ./
COPY test/ ./test/

# Start script that runs Ollama + the Python app
COPY start.sh .
RUN chmod +x start.sh

EXPOSE 8080
ENV PYTHONUNBUFFERED=1
ENV PORT=8080
ENV OLLAMA_HOST=0.0.0.0
# Use smaller model by default — fits Railway CPU instances
# Change to 'qwen2.5-vl:7b-instruct-q4_K_M' if you have 8GB+ RAM
ENV OLLAMA_MODEL=qwen2.5-vl:3b-instruct-q4_K_M

CMD ["./start.sh"]
