# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm@sha256:93ab4b7fa528b25124c97bcc755415e60eb671a86b4dbe0328df2fe2d1c1193d AS build
WORKDIR /build
COPY apps/analytics/requirements.txt ./
RUN pip install --no-cache-dir --target=/build/deps -r requirements.txt
COPY apps/analytics/app.py ./
RUN python -m py_compile app.py

FROM python:3.12-slim-bookworm@sha256:93ab4b7fa528b25124c97bcc755415e60eb671a86b4dbe0328df2fe2d1c1193d AS runtime
LABEL org.opencontainers.image.title="ExecRelay Analytics" \
      org.opencontainers.image.description="ExecRelay analytics query service" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/ninadk/execrelay"
RUN groupadd --system execrelay && useradd --system --gid execrelay --home-dir /app --shell /usr/sbin/nologin execrelay
WORKDIR /app
COPY --from=build /build/deps /app/deps
COPY --from=build /build/app.py /app/app.py
RUN chown -R execrelay:execrelay /app
USER execrelay
EXPOSE 8080
ENV HTTP_ADDR=0.0.0.0:8080 PYTHONPATH=/app/deps
HEALTHCHECK --interval=10s --timeout=2s --retries=3 CMD ["python", "/app/app.py", "--healthcheck"]
CMD ["python", "/app/app.py"]
