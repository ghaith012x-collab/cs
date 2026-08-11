FROM python:3.11-slim

WORKDIR /app

# System dependencies for Thorium + Tor
RUN apt-get update && apt-get install -y \
    wget gnupg curl libglib2.0-0 libnss3 libnspr4 libdbus-1-3 \
    libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
    libasound2 libcairo2 libpango-1.0-0 libgtk-3-0 libgtk-4-1 \
    libexpat1 libx11-6 libxcb1 libxext6 libvulkan1 libu2f-udev \
    ca-certificates fonts-liberation libatspi2.0-0 \
    libcurl4 libcurl3-gnutls xdg-utils tor \
    && rm -rf /var/lib/apt/lists/*

# Install Thorium M138 (Chromium 138 — latest with Linux .deb builds)
RUN wget -q -O /tmp/thorium.deb \
    https://github.com/Alex313031/thorium/releases/download/M138.0.7204.303/thorium-browser_138.0.7204.303_AVX2.deb && \
    apt-get update && apt-get install -y /tmp/thorium.deb && \
    rm /tmp/thorium.deb && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy ALL application files
COPY *.py ./
COPY *.txt ./
COPY config.json ./
COPY models/ models/
COPY torrc /etc/tor/torrc
COPY start.sh .
RUN chmod +x start.sh

EXPOSE 8080
ENV PYTHONUNBUFFERED=1
ENV PORT=8080
# Uncomment to force truedriver + Thorium:
# ENV ENGINE=truedriver

CMD ["./start.sh"]
