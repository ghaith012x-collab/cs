FROM python:3.11-slim

WORKDIR /app

# System dependencies for the SeleniumBase CDP engine (Brave / unbranded
# Chromium) + Xvfb virtual display + Tor
RUN apt-get update && apt-get install -y \
    wget gnupg curl libglib2.0-0 libnss3 libnspr4 libdbus-1-3 \
    libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
    libasound2 libcairo2 libpango-1.0-0 libgtk-3-0 libgtk-4-1 \
    libexpat1 libx11-6 libxcb1 libxext6 libvulkan1 libu2f-udev \
    ca-certificates fonts-liberation libatspi2.0-0 \
    libcurl4 libcurl3-gnutls xdg-utils tor unzip \
    xvfb xauth \
    && rm -rf /var/lib/apt/lists/*

# Install Brave Browser (unbranded Chromium) from Brave's official apt repo.
# The SeleniumBase CDP engine resolves it via /usr/bin/brave-browser or
# BRAVE_BINARY, and runs it with --incognito ALWAYS on.
RUN curl -fsSLo /usr/share/keyrings/brave-browser-archive-keyring.gpg \
        https://brave-browser-apt-release.s3.brave.com/brave-browser-archive-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/brave-browser-archive-keyring.gpg arch=amd64] https://brave-browser-apt-release.s3.brave.com stable main" \
        > /etc/apt/sources.list.d/brave-browser-release.list \
    && apt-get update \
    && apt-get install -y brave-browser \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Pre-bake the chromedriver + UC driver matching Brave's Chromium major
# version so the first worker launch doesn't stall on a cold download
# (best-effort — SeleniumBase auto-downloads a matching driver on first
# launch if these can't).
RUN SB_MAJOR="$(brave-browser --version 2>/dev/null | grep -oE '[0-9]+' | head -1)" \
    && sbase install chromedriver "$SB_MAJOR" 2>/dev/null || true
RUN sbase install uc_driver 2>/dev/null || true

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
CMD ["./start.sh"]
