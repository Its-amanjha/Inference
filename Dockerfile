FROM python:3.13-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    INFERENCE_DATA_DIR=/data \
    INFERENCE_FRONTEND_DIR=/app/frontend
WORKDIR /app

COPY requirements-linux-amd64.lock ./
RUN python -m pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements-linux-amd64.lock \
    && python -m pip check \
    && groupadd --gid 10001 inference \
    && useradd --uid 10001 --gid inference --no-create-home inference \
    && mkdir /data \
    && chown inference:inference /data
COPY backend ./backend
COPY frontend ./frontend

USER 10001:10001
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3000/healthz', timeout=3).read()"
CMD ["python", "-m", "uvicorn", "backend.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "3000", "--workers", "1", "--no-proxy-headers"]
