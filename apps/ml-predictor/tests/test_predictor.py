"""Unit tests for the XGBoost predictor and its Option-1 decision logic.

Loads the real shipped model artifact (validates feature_order + model), then
pins the win probability via monkeypatch to exercise each branch of the
open/close/flip state machine deterministically.
"""

from pathlib import Path

import pytest

# Import the ported predictor module from the parent app directory.
import sys

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

from xgb_predictor import XGBPredictor  # noqa: E402

MODEL_DIR = APP_DIR / "model"
MODEL_PATH = str(MODEL_DIR / "xgb_production.json")
FEATURE_ORDER_PATH = str(MODEL_DIR / "feature_order.txt")


@pytest.fixture(scope="module")
def predictor() -> XGBPredictor:
    return XGBPredictor(MODEL_PATH, FEATURE_ORDER_PATH, threshold=0.50)


def _features(pred: XGBPredictor) -> dict:
    """A complete feature dict (all model features except `direction`) set to 0."""
    return {name: 0.0 for name in pred.feature_order if name != "direction"}


def _pin_prob(pred: XGBPredictor, value: float, monkeypatch) -> None:
    monkeypatch.setattr(pred, "_score", lambda x: value)


def test_model_loads_with_expected_feature_count(predictor):
    assert len(predictor.feature_order) == 36
    assert predictor.feature_order[0] == "direction"


def test_model_version_is_sha256_prefix_of_artifact_bytes(predictor):
    import hashlib

    with open(MODEL_PATH, "rb") as f:
        expected = hashlib.sha256(f.read()).hexdigest()[:12]
    assert predictor.model_version == expected
    assert len(predictor.model_version) == 12


def test_open_long_when_flat_and_filter_passes(predictor, monkeypatch):
    _pin_prob(predictor, 0.80, monkeypatch)
    out = predictor.predict(
        {"direction": 1, "features": _features(predictor)}, current_position=None
    )
    assert out["action_summary"] == "OPEN_LONG"
    assert out["should_open"] is True
    assert out["should_close"] is False
    assert out["open_direction"] == "LONG"
    assert out["error"] is None
    assert out["model_version"] == predictor.model_version


def test_skip_when_flat_and_filter_fails(predictor, monkeypatch):
    _pin_prob(predictor, 0.20, monkeypatch)
    out = predictor.predict(
        {"direction": 1, "features": _features(predictor)}, current_position=None
    )
    assert out["action_summary"] == "NOTHING"
    assert out["should_open"] is False


def test_flip_when_opposite_signal_passes_filter(predictor, monkeypatch):
    _pin_prob(predictor, 0.90, monkeypatch)
    out = predictor.predict(
        {"direction": -1, "features": _features(predictor)}, current_position="LONG"
    )
    assert out["action_summary"] == "FLIP_SHORT"
    assert out["should_close"] is True
    assert out["should_open"] is True


def test_close_only_when_opposite_signal_fails_filter(predictor, monkeypatch):
    _pin_prob(predictor, 0.10, monkeypatch)
    out = predictor.predict(
        {"direction": -1, "features": _features(predictor)}, current_position="LONG"
    )
    assert out["action_summary"] == "CLOSE_ONLY"
    assert out["should_close"] is True
    assert out["should_open"] is False


def test_no_pyramiding_on_same_direction(predictor, monkeypatch):
    _pin_prob(predictor, 0.99, monkeypatch)
    out = predictor.predict(
        {"direction": 1, "features": _features(predictor)}, current_position="LONG"
    )
    assert out["action_summary"] == "NOTHING"
    assert out["should_open"] is False
    assert out["should_close"] is False


def test_invalid_direction_returns_error(predictor):
    out = predictor.predict(
        {"direction": 0, "features": _features(predictor)}, current_position=None
    )
    assert out["error"] is not None
    assert out["action_summary"] == "NOTHING"
    assert out["model_version"] == predictor.model_version


def test_missing_features_returns_error(predictor):
    out = predictor.predict({"direction": 1, "features": {"ret_1": 0.0}})
    assert out["error"] is not None
    assert "Missing features" in out["error"]
    assert out["model_version"] == predictor.model_version
