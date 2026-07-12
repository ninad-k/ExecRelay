"""Golden-payload contract test: locks the TradingView Pine <-> ml-predictor
feature contract together.

The `features` object in a TradingView alert (see pine/README.md) must
contain exactly the 35 entries of model/feature_order.txt minus the
server-injected `direction`. If either side drifts -- someone edits the Pine
script's feature set, or someone edits feature_order.txt / swaps the model --
this test fails, because it runs the *real* shipped model against a fixture
that mirrors the documented payload example verbatim.

This is the CI tripwire: it is intentionally NOT mocked.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

from xgb_predictor import XGBPredictor  # noqa: E402

MODEL_DIR = APP_DIR / "model"
MODEL_PATH = str(MODEL_DIR / "xgb_production.json")
FEATURE_ORDER_PATH = str(MODEL_DIR / "feature_order.txt")

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "tradingview_alert.json"

KNOWN_ACTION_SUMMARIES = {
    "OPEN_LONG",
    "OPEN_SHORT",
    "FLIP_LONG",
    "FLIP_SHORT",
    "CLOSE_ONLY",
    "NOTHING",
}


@pytest.fixture(scope="module")
def predictor() -> XGBPredictor:
    return XGBPredictor(MODEL_PATH, FEATURE_ORDER_PATH, threshold=0.50)


@pytest.fixture(scope="module")
def alert() -> dict:
    with open(FIXTURE_PATH) as f:
        return json.load(f)


def test_fixture_matches_documented_payload_shape(alert):
    assert alert["action"] in ("buy", "sell")
    assert alert["use_xgb"] is True
    assert isinstance(alert["features"], dict)


def test_fixture_features_match_feature_order_exactly(predictor, alert):
    """The lockstep guard: fixture features must be EXACTLY
    feature_order.txt minus `direction` -- no more, no fewer, no typos."""
    expected = {name for name in predictor.feature_order if name != "direction"}
    actual = set(alert["features"].keys())
    assert actual == expected


def test_real_model_scores_the_golden_payload(predictor, alert):
    direction = 1 if alert["action"] == "buy" else -1
    out = predictor.predict({"direction": direction, "features": alert["features"]})

    assert out["error"] is None
    assert out["prob_win"] is not None
    assert 0 < out["prob_win"] < 1
    assert out["action_summary"] in KNOWN_ACTION_SUMMARIES
    assert out["model_version"]
    assert out["model_version"] == predictor.model_version
