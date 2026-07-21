FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    playwright==1.40.0 \
    opencv-python-headless==4.9.0.80 \
    numpy==1.26.4 \
    aiofiles==23.1.0 && \
    pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.1.0

RUN python -m playwright install chromium

COPY app.py server.py captcha_solver.py requirements.txt ./
COPY test/ ./test/

RUN chmod +r ./test/site.html

EXPOSE 8080

ENV PYTHONUNBUFFERED=1

CMD ["python", "app.py", "--headless"]