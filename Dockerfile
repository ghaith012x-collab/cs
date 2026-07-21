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

RUN pip install --no-cache-dir \
    playwright==1.40.0 \
    opencv-python-headless==4.9.0.80 \
    numpy==1.26.4 \
    aiofiles==23.1.0 \
    aiohttp==3.9.1 && \
    pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.1.0

RUN python -m playwright install chromium

COPY app.py server.py captcha_solver.py requirements.txt ./
COPY test/ ./test/

RUN chmod +r ./test/site.html

EXPOSE 5000

ENV PYTHONUNBUFFERED=1

CMD ["xvfb-run", "-a", "python", "app.py"]