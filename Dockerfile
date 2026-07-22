FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    xvfb \
    libglib2.0-0 \
    libnss3 \
    libnspr4 \
    libdbus-1-3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libcairo2 \
    libpango-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Install torch+torchvision CPU first (pinned compatible versions)
RUN pip install --no-cache-dir \
    torch==2.2.0+cpu torchvision==0.17.0+cpu --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir \
    open-clip-torch==2.24.0 \
    playwright==1.40.0 \
    opencv-python-headless==4.9.0.80 \
    numpy==1.26.4 \
    aiofiles==23.1.0 \
    aiohttp==3.9.1 \
    Pillow==10.2.0

RUN python -m playwright install chromium

COPY app.py server.py captcha_solver.py config.json requirements.txt ./
COPY test/ ./test/
RUN chmod +r ./test/site.html 2>/dev/null || true

EXPOSE 8080
ENV PYTHONUNBUFFERED=1
ENV PORT=8080

CMD ["python", "-u", "app.py"]
