"""Unit tests for the backtester's ported copy of XGBPredictor.

Mirrors apps/ml-predictor/tests/test_predictor.py -- this copy must behave
identically to the live predictor's Option-1 decision logic, since the whole
point of the backtest harness is to reuse that exact logic offline. The
model artifact itself is loaded from apps/ml-predictor/model (not duplicated
into apps/backtester's git tree -- see xgb_predictor.py's module docstring).
"""

from pathlib import Path

import pytest

from xgb_predictor import XGBPredictor

MODEL_DIR = Path(__file__).resolve().parents[2] / "ml-predictor" / "model"
MODEL_PATH = str(MODEL_DIR / "xgb_production.json")
FEATURE_ORDER_PATH = str(MODEL_DIR / "feature_order.txt")


@pytest.fixture(scope="module")
def predictor() -> XGBPredictor:
    return XGBPredictor(MODEL_PATH, FEATURE_ORDER_PATH, threshold=0.50)


def _features(pred: XGBPredictor) -> dict:
    return {name: 0.0 for name in pred.feature_order if name != "direction"}


def _pin_prob(pred: XGBPredictor, value: float, monkeypatch) -> None:
    monkeypatch.setattr(pred, "_score", lambda x: value)


def test_model_loads_with_expected_feature_count(predictor):
    assert len(predictor.feature_order) == 36
    assert predictor.feature_order[0] == "direction"


def test_open_long_when_flat_and_filter_passes(predictor, monkeypatch):
    _pin_prob(predictor, 0.80, monkeypatch)
    out = predictor.predict(
        {"direction": 1, "features": _features(predictor)}, current_position=None
    )
    assert out["action_summary"] == "OPEN_LONG"
    assert out["should_open"] is True


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
