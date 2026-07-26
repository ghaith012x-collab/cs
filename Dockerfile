FROM python:3.11-slim

WORKDIR /app

# Minimal system dependencies — just Playwright + Tor
RUN apt-get update && apt-get install -y \
    wget gnupg libglib2.0-0 libnss3 libnspr4 libdbus-1-3 \
    libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
    libasound2 libcairo2 libpango-1.0-0 curl tor \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
RUN pip install --no-cache-dir \
    playwright==1.40.0 \
    aiohttp==3.9.1 \
    Pillow==10.2.0 \
    asyncpg>=0.29.0 \
    requests>=2.31.0

RUN python -m playwright install chromium

# Copy setup script first, then download NopeCHA extension
COPY setup_extensions.py requirements.txt ./
RUN python setup_extensions.py

# Copy application files
COPY app.py server.py captcha_solver.py solver_api.py database.py config.json ./
COPY torrc /etc/tor/torrc
COPY start.sh .
RUN chmod +x start.sh

EXPOSE 8080
ENV PYTHONUNBUFFERED=1
ENV PORT=8080

CMD ["./start.sh"]
