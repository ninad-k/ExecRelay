FROM python:3.12-slim

WORKDIR /app

# libgomp1 is required at runtime by the xgboost wheel (OpenMP).
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY apps/ml-predictor/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY apps/ml-predictor/app.py ./app.py
COPY apps/ml-predictor/xgb_predictor.py ./xgb_predictor.py
COPY apps/ml-predictor/model/ ./model/

# Fail the build if the shipped artifacts don't match their recorded
# checksums (e.g. a model swap that forgot to update model/SHA256SUMS).
RUN cd model && sha256sum -c SHA256SUMS

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health').read()"

CMD ["python3", "app.py"]
