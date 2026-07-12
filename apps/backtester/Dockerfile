FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY apps/backtester/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY apps/backtester/app.py ./app.py

# ML-filter backtest harness (offline CLI tool, not served over HTTP -- see
# backtest_ml.py's module docstring). The trained model artifact is owned by
# apps/ml-predictor; it's copied in here (read-only, build-context copy, not
# a git-tracked duplicate) so the harness can load it without a cross-app
# import at runtime.
COPY apps/backtester/feature_builder.py ./feature_builder.py
COPY apps/backtester/xgb_predictor.py ./xgb_predictor.py
COPY apps/backtester/backtest_ml.py ./backtest_ml.py
COPY apps/ml-predictor/model ./model

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health').read()"

CMD ["python3", "app.py"]
