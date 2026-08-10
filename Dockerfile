FROM python:3.11-slim

WORKDIR /app

# System dependencies for Playwright + Tor + Mullvad VPN
RUN apt-get update && apt-get install -y \
    wget gnupg curl libglib2.0-0 libnss3 libnspr4 libdbus-1-3 \
    libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
    libasound2 libcairo2 libpango-1.0-0 tor \
    && rm -rf /var/lib/apt/lists/*

# Install Mullvad VPN CLI (for IP rotation via WireGuard)
# Needs --cap-add=NET_ADMIN --device=/dev/net/tun at Docker run time.
RUN curl -fsSLo /usr/share/keyrings/mullvad-keyring.asc \
        https://repository.mullvad.net/deb/mullvad-keyring.asc && \
    echo "deb [signed-by=/usr/share/keyrings/mullvad-keyring.asc arch=$(dpkg --print-architecture)] https://repository.mullvad.net/deb/stable stable main" \
        > /etc/apt/sources.list.d/mullvad.list && \
    apt-get update && apt-get install -y mullvad-vpn && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium for Playwright + Patchright (stealth engine)
RUN python -m playwright install chromium
RUN python -m patchright install chromium || true

# Copy ALL application files (glob keeps future files in sync)
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
