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

# Download NopeCHA extension (free hCaptcha solver, no API key needed, 100 solves/day)
RUN python -c "
import requests, zipfile, os, shutil
from pathlib import Path
print('[Build] Downloading NopeCHA extension...')
r = requests.get('https://github.com/NopeCHALLC/nopecha-extension/releases/latest/download/chromium_automation.zip', timeout=60, allow_redirects=True)
r.raise_for_status()
Path('extensions').mkdir(exist_ok=True)
with open('extensions/nopecha.zip', 'wb') as f: f.write(r.content)
with zipfile.ZipFile('extensions/nopecha.zip', 'r') as z: z.extractall('extensions/nopecha')
os.unlink('extensions/nopecha.zip')
print(f'[Build] NopeCHA extension ready ({os.path.isdir("extensions/nopecha") and os.path.isfile("extensions/nopecha/manifest.json")})')
"

# Copy application files
COPY app.py server.py captcha_solver.py solver_api.py database.py setup_extensions.py config.json requirements.txt ./
COPY extensions/ ./extensions/
COPY torrc /etc/tor/torrc
COPY start.sh .
RUN chmod +x start.sh

EXPOSE 8080
ENV PYTHONUNBUFFERED=1
ENV PORT=8080

CMD ["./start.sh"]
