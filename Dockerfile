FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/app/data/organizador.db \
    DASHBOARD_HOST=0.0.0.0 \
    DASHBOARD_PORT=8000 \
    SECURE_COOKIES=true

WORKDIR /app
RUN useradd --create-home --uid 1000 app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY organizador ./organizador
RUN mkdir -p /app/data && chown -R app:app /app

USER app
# Banco SQLite: monte um volume persistente aqui (Easypanel → Mounts)
VOLUME ["/app/data"]
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

CMD ["python", "-m", "organizador", "serve"]
