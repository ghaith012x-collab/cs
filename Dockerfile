FROM python:3.9-slim-buster

WORKDIR /app

RUN pip install --no-cache-dir open-clip-torch>=2.24.0 torch>=2.1.0 --index-url https://download.pytorch.org/whl/cpu Pillow>=10.0.0 numpy>=1.26.0 opencv-python-headless>=4.9.0 playwright>=1.40.0 aiohttp>=3.8.0 aiofiles>=23.1.0

COPY . .

CMD ["python", "server.py"]
