from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from typing import Any

import asyncpg
import numpy as np
from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import StandardScaler

SERVICE = "ml-predictor"
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
DB_DSN = os.environ.get("DATABASE_URL",
                        "postgresql://execrelay:execrelay_dev_password@postgres:5432/execrelay")

logger = logging.getLogger(SERVICE)
DEBUG = os.environ.get("DEBUG", "true").lower() in ("true", "1", "yes", "on")
log_level = logging.DEBUG if DEBUG else logging.INFO
logging.basicConfig(level=log_level,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s",
                    stream=sys.stdout)

if DEBUG:
    logger.info("Debug logging enabled")

# Prometheus metrics
predictions_made = Counter("ml_predictions_total", "Total predictions made", ["model"])
prediction_latency = Histogram("ml_prediction_latency_seconds", "Prediction latency", ["model"])
model_training_runs = Counter("ml_training_runs_total", "Total model training runs")
model_accuracy = Gauge("ml_model_accuracy", "Model accuracy on test set", ["model"])

# Global model state
model = None
scaler = None
feature_names = [
    "time_of_day_hour", "day_of_week", "symbol_volatility",
    "signal_frequency", "win_rate_pct", "account_drawdown_pct",
    "portfolio_correlation_exposure"
]


async def train_model(pool: asyncpg.Pool) -> tuple[RandomForestClassifier, StandardScaler]:
    """Train ML model on historical signal data."""
    try:
        async with pool.acquire() as conn:
            # Get training data: signals with features and fills outcome
            rows = await conn.fetch(
                """
                SELECT
                    sf.time_of_day_hour,
                    sf.day_of_week,
                    sf.symbol_volatility,
                    sf.signal_frequency,
                    sf.win_rate_pct,
                    sf.account_drawdown_pct,
                    sf.portfolio_correlation_exposure,
                    CASE WHEN f.status = 'completed' THEN 1 ELSE 0 END as fill_success
                FROM signal_features sf
                LEFT JOIN fills f ON sf.signal_id = f.signal_id
                WHERE sf.created_at > now() - interval '30 days'
                LIMIT 1000
                """
            )

        if len(rows) < 10:
            logger.warning("insufficient training data, using default model")
            # Return untrained but initialized model
            clf = RandomForestClassifier(n_estimators=10, random_state=42)
            scaler = StandardScaler()
            return clf, scaler

        X = []
        y = []
        for row in rows:
            X.append([
                float(row["time_of_day_hour"] or 0),
                float(row["day_of_week"] or 0),
                float(row["symbol_volatility"] or 0),
                float(row["signal_frequency"] or 0),
                float(row["win_rate_pct"] or 0),
                float(row["account_drawdown_pct"] or 0),
                float(row["portfolio_correlation_exposure"] or 0),
            ])
            y.append(row["fill_success"])

        X_array = np.array(X)
        y_array = np.array(y)

        # Split: 80% train, 20% test
        split_idx = int(len(X_array) * 0.8)
        X_train, X_test = X_array[:split_idx], X_array[split_idx:]
        y_train, y_test = y_array[:split_idx], y_array[split_idx:]

        # Scale features
        scaler = StandardScaler()
        X_train_scaled = scaler.fit_transform(X_train)
        X_test_scaled = scaler.transform(X_test)

        # Train model
        clf = RandomForestClassifier(n_estimators=50, random_state=42, max_depth=10)
        clf.fit(X_train_scaled, y_train)

        # Evaluate
        accuracy = clf.score(X_test_scaled, y_test)
        logger.info(f"model trained: accuracy={accuracy:.2%} on {len(X_test)} test samples")
        model_accuracy.labels(model="signal_success").set(accuracy)

        # Store model version in DB
        model_version = datetime.utcnow().isoformat()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE ml_models SET is_active = FALSE WHERE model_type = 'signal_success_predictor'
                """
            )
            await conn.execute(
                """
                INSERT INTO ml_models (model_type, model_version, training_date, metrics, is_active)
                VALUES ('signal_success_predictor', $1, NOW(), $2, TRUE)
                """,
                model_version,
                json.dumps({"accuracy": float(accuracy), "train_samples": len(X_train), "test_samples": len(X_test)})
            )

        model_training_runs.inc()
        return clf, scaler

    except Exception as exc:
        logger.error(f"train_model error: {exc}")
        # Return default model
        return RandomForestClassifier(n_estimators=10, random_state=42), StandardScaler()


async def predict_signal_success(features: dict[str, float]) -> float:
    """Predict probability of signal fill success (0-1)."""
    global model, scaler

    if model is None or scaler is None:
        return 0.5  # Default confidence if model not loaded

    try:
        X = np.array([[
            float(features.get("time_of_day_hour", 0)),
            float(features.get("day_of_week", 0)),
            float(features.get("symbol_volatility", 0)),
            float(features.get("signal_frequency", 0)),
            float(features.get("win_rate_pct", 0)),
            float(features.get("account_drawdown_pct", 0)),
            float(features.get("portfolio_correlation_exposure", 0)),
        ]])

        X_scaled = scaler.transform(X)
        confidence = float(model.predict_proba(X_scaled)[0][1])
        return confidence

    except Exception as exc:
        logger.error(f"predict error: {exc}")
        return 0.5


async def http_handler(reader, writer):
    """HTTP server for health, metrics, and inference."""
    request_line = (await reader.readline()).decode().strip()
    if not request_line:
        writer.close()
        return

    parts = request_line.split()
    if len(parts) < 2:
        writer.close()
        return

    method = parts[0]
    path = parts[1]

    if path == "/health" and method == "GET":
        response = b"HTTP/1.1 200 OK\r\nContent-Length: 18\r\nContent-Type: application/json\r\n\r\n{\"status\":\"ok\"}"

    elif path == "/metrics" and method == "GET":
        metrics_data = generate_latest()
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: " + CONTENT_TYPE_LATEST.encode() + b"\r\n"
            b"Content-Length: " + str(len(metrics_data)).encode() + b"\r\n"
            b"\r\n"
        ) + metrics_data

    elif path == "/predict" and method == "POST":
        try:
            # Read POST body
            content_length = 0
            headers = {}
            while True:
                line = (await reader.readline()).decode().strip()
                if not line:
                    break
                if ":" in line:
                    key, val = line.split(":", 1)
                    headers[key.lower()] = val.strip()
                    if key.lower() == "content-length":
                        content_length = int(val.strip())

            body = b""
            if content_length > 0:
                body = await reader.readexactly(content_length)

            features = json.loads(body.decode())

            # Make prediction
            import time
            start = time.time()
            confidence = await predict_signal_success(features)
            latency = time.time() - start
            prediction_latency.labels(model="signal_success").observe(latency)
            predictions_made.labels(model="signal_success").inc()

            result = json.dumps({"confidence": confidence, "timestamp": datetime.utcnow().isoformat()})
            response_body = result.encode()
            response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: " + str(len(response_body)).encode() + b"\r\n"
                b"\r\n"
            ) + response_body

        except Exception as exc:
            logger.error(f"predict endpoint error: {exc}")
            response = b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\n\r\n"

    else:
        response = b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n"

    writer.write(response)
    await writer.drain()
    writer.close()


async def main():
    global model, scaler

    logger.info(f"starting {SERVICE} service")

    pool = await asyncpg.create_pool(DB_DSN, min_size=2, max_size=10)
    if not pool:
        logger.error("failed to create database pool")
        sys.exit(1)

    try:
        # Train model on startup
        logger.info("training model on startup...")
        model, scaler = await train_model(pool)
        logger.info("model training complete")

        # Start HTTP server
        server = await asyncio.start_server(http_handler, "0.0.0.0", HTTP_PORT)
        logger.info(f"HTTP server listening on :{HTTP_PORT}")

        # Setup signal handlers
        loop = asyncio.get_event_loop()

        def shutdown():
            logger.info(f"shutting down {SERVICE}")
            loop.stop()

        import signal as signal_module
        for sig in [signal_module.SIGINT, signal_module.SIGTERM]:
            loop.add_signal_handler(sig, shutdown)

        # Optional: daily model retraining task
        async def retrain_task():
            while True:
                try:
                    await asyncio.sleep(24 * 3600)  # Every 24 hours
                    logger.info("retraining model...")
                    new_model, new_scaler = await train_model(pool)
                    model = new_model
                    scaler = new_scaler
                    logger.info("model retraining complete")
                except Exception as exc:
                    logger.error(f"retrain_task error: {exc}")

        asyncio.create_task(retrain_task())

        async with server:
            await server.serve_forever()

    except Exception as exc:
        logger.error(f"error in main: {exc}")
    finally:
        await pool.close() if pool else None


if __name__ == "__main__":
    asyncio.run(main())
