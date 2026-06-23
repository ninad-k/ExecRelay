"""XGBoost predictor for live trade filtering.

Ported from the standalone MT5 webhook system into the ExecRelay ml-predictor
service. Loads a pre-trained XGBoost model from disk and applies "Option 1"
close/open guidance:

  - ANY opposite signal closes the current position (no filter applied to close)
  - New positions only open if the model's win probability clears the threshold
  - A reversal (flip) happens only when the new signal itself passes the filter

The model is trained offline and shipped as an artifact (model/xgb_production.json
plus model/feature_order.txt). This module does no training and needs no database
connection — inference is a pure function of the request payload.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import xgboost as xgb

logger = logging.getLogger("ml-predictor.xgb")

# The model expects exactly this many features (including the injected
# `direction`). Guards against a stale feature_order.txt / model mismatch.
EXPECTED_FEATURE_COUNT = 36


class XGBPredictor:
    """Loads a trained XGBoost model and provides Option-1 close/open guidance."""

    def __init__(
        self, model_path: str, feature_order_path: str, threshold: float = 0.50
    ):
        self.threshold = threshold
        self.model = xgb.XGBClassifier()
        self.model.load_model(model_path)

        with open(feature_order_path) as f:
            self.feature_order = [line.strip() for line in f if line.strip()]
        if len(self.feature_order) != EXPECTED_FEATURE_COUNT:
            raise ValueError(
                f"Expected {EXPECTED_FEATURE_COUNT} features, "
                f"got {len(self.feature_order)} from {feature_order_path}"
            )

        logger.info(
            "XGBPredictor loaded: model=%s features=%d threshold=%.2f",
            Path(model_path).name,
            len(self.feature_order),
            threshold,
        )

    def _build_feature_vector(self, features_dict: dict, direction: int) -> np.ndarray:
        merged = {**features_dict, "direction": direction}
        missing = [f for f in self.feature_order if f not in merged]
        if missing:
            raise KeyError(f"Missing features: {missing}")
        return np.array(
            [[float(merged[name]) for name in self.feature_order]],
            dtype=np.float32,
        )

    def predict(self, payload: dict, current_position: str | None = None) -> dict:
        """Run inference and return a trade decision.

        Args:
            payload: dict with `direction` (1 or -1) and `features` (dict of the
                35 model features, excluding the injected `direction`).
            current_position: "LONG" / "SHORT" / None — the caller tracks this.

        Returns:
            dict with: signal_direction, prob_win, threshold, should_close,
            should_open, open_direction, action_summary
            (OPEN_LONG / OPEN_SHORT / FLIP_LONG / FLIP_SHORT / CLOSE_ONLY /
            NOTHING), reason, timestamp, and error (None unless something broke).
        """
        result = {
            "signal_direction": None,
            "prob_win": None,
            "threshold": self.threshold,
            "should_close": False,
            "should_open": False,
            "open_direction": None,
            "action_summary": "NOTHING",
            "reason": "",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "error": None,
        }

        try:
            direction = int(payload.get("direction", 0))
            if direction not in (1, -1):
                raise ValueError(f"direction must be 1 or -1, got {direction}")
            sig_dir = "LONG" if direction == 1 else "SHORT"
            result["signal_direction"] = sig_dir

            features = payload.get("features")
            if not isinstance(features, dict):
                raise ValueError("payload must include 'features' dict")

            x = self._build_feature_vector(features, direction)
            prob = float(self.model.predict_proba(x)[0, 1])
            result["prob_win"] = round(prob, 4)
            filter_pass = prob > self.threshold

            # ===== OPTION 1 CLOSE/OPEN LOGIC =====
            # 1) Close current position if signal is opposite (no filter on close)
            if current_position == "LONG" and sig_dir == "SHORT":
                result["should_close"] = True
            elif current_position == "SHORT" and sig_dir == "LONG":
                result["should_close"] = True

            # 2) Open new position only if (a) flat or just closed AND (b) filter passes
            will_be_flat = current_position is None or result["should_close"]
            same_direction = current_position == sig_dir

            if same_direction:
                pass  # don't pyramid
            elif will_be_flat and filter_pass:
                result["should_open"] = True
                result["open_direction"] = sig_dir

            # 3) Build action_summary
            if result["should_close"] and result["should_open"]:
                result["action_summary"] = f"FLIP_{sig_dir}"
                result["reason"] = (
                    f"Close {current_position}, open {sig_dir} "
                    f"(prob {prob:.3f} > {self.threshold:.2f})"
                )
            elif result["should_close"]:
                result["action_summary"] = "CLOSE_ONLY"
                result["reason"] = (
                    f"Close {current_position}, no new entry "
                    f"({sig_dir} prob {prob:.3f} <= {self.threshold:.2f})"
                )
            elif result["should_open"]:
                result["action_summary"] = f"OPEN_{sig_dir}"
                result["reason"] = (
                    f"Flat -> open {sig_dir} (prob {prob:.3f} > {self.threshold:.2f})"
                )
            elif same_direction:
                result["reason"] = f"Already in {current_position}, ignore"
            else:
                result["reason"] = (
                    f"Flat, {sig_dir} prob {prob:.3f} <= {self.threshold:.2f} -> skip"
                )

            logger.info(
                "sig=%s prob=%.4f pos=%s -> %s | %s",
                sig_dir,
                prob,
                current_position,
                result["action_summary"],
                result["reason"],
            )

        except Exception as e:  # noqa: BLE001 - surfaced to caller via result["error"]
            result["error"] = str(e)
            logger.exception("Prediction failed")

        return result
