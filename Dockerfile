# Permit Route server (API only; the app in ../frontend is hosted separately).
#   cd server
#   docker build -t permit-route-server .
#   docker run -p 8000:8000 -e APP_PASSWORD=... -e APP_SECRET=... -v permit-data:/data permit-route-server
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    OVERPASS_CACHE_DIR=/data/overpass_cache

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py route.schema.json ./

RUN useradd --create-home appuser && mkdir -p /data && chown appuser /data
USER appuser
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://localhost:{os.environ[\"PORT\"]}/health')"

# hosting services (Render, Cloud Run, Railway, ...) set PORT; HTTPS is handled by the host
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
