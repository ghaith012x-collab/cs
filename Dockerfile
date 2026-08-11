FROM python:3.11-slim

WORKDIR /app

# System dependencies for Chromium (Clearcote) + Tor
RUN apt-get update && apt-get install -y \
    wget gnupg curl libglib2.0-0 libnss3 libnspr4 libdbus-1-3 \
    libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
    libasound2 libcairo2 libpango-1.0-0 libgtk-3-0 libgtk-4-1 \
    libexpat1 libx11-6 libxcb1 libxext6 libvulkan1 libu2f-udev \
    ca-certificates fonts-liberation libatspi2.0-0 \
    libcurl4 libcurl3-gnutls xdg-utils tor \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Pre-fetch + verify the Clearcote stealth Chromium binary into the image cache
# (the truedriver driver launches this binary; fingerprint personas are engine-level).
RUN python -c "from clearcote import executable_path; print('Clearcote binary:', executable_path(quiet=True))"

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
