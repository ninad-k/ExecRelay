FROM python:3.12-slim

LABEL org.opencontainers.image.title="ExecRelay Risk Service" \
      org.opencontainers.image.description="Risk exposure tracking and limit enforcement" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/ninadk/execrelay"

WORKDIR /app
RUN pip install --no-cache-dir asyncpg nats-py prometheus-client
COPY apps/risk/app.py .
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=2s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health').read()" || exit 1
CMD ["python", "app.py"]
