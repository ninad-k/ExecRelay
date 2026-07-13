"""Demo E2E decision-matrix runner — fires signals, asserts MT5 demo positions.

Each case posts to a locally running ingress and then asserts the resulting
position state in the attached MT5 demo terminal (via the shim's magic
number). Cases assume a specific ingress/predictor mode — the runbook at
docs/development/demo-e2e-test.md orchestrates the sequence and the required
service restarts between cases.

Usage: python scripts/e2e_demo_matrix.py case1|case2|...|case7
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import MetaTrader5 as mt5

ROOT = Path(__file__).resolve().parent.parent
INGRESS = os.environ.get("EXECRELAY_INGRESS", "http://127.0.0.1:8081")
LICENSE = os.environ.get("EXECRELAY_TEST_LICENSE", "60000000001")
SECRET = os.environ.get("EXECRELAY_TEST_SECRET", "test-secret")
SYMBOL = os.environ.get("EXECRELAY_TEST_SYMBOL", "BTCUSD")
MAGIC = int(os.environ.get("EA_SHIM_MAGIC", "20240101"))
SETTLE_SECS = 4

FEATURES = json.load(
    open(ROOT / "apps/ml-predictor/tests/fixtures/tradingview_alert.json")
)["features"]


def ml_request(action, current_position=None):
    body = {
        "license_id": LICENSE,
        "secret": SECRET,
        "action": action,
        "symbol": SYMBOL,
        "volume": 0.01,
        "sl": 0,
        "tp": 0,
        "comment": f"matrix-{action}",
        "features": FEATURES,
    }
    if current_position:
        body["current_position"] = current_position
    req = urllib.request.Request(
        f"{INGRESS}/webhook/ml",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as r:
        return r.status, json.loads(r.read())


def flat_request(command):
    body = f"{LICENSE},{command},{SYMBOL},vol_lots=0.01,secret={SECRET}"
    req = urllib.request.Request(
        f"{INGRESS}/webhook",
        data=body.encode(),
        headers={"Content-Type": "text/plain"},
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def positions():
    mt5.initialize()
    return [
        (p.ticket, "BUY" if p.type == 0 else "SELL", p.volume)
        for p in (mt5.positions_get(symbol=SYMBOL) or [])
        if p.magic == MAGIC
    ]


def check(name, expect_status, resp_status, resp, want_positions):
    time.sleep(SETTLE_SECS)
    pos = positions()
    kinds = sorted(k for _, k, _ in pos)
    ok = resp["status"] == expect_status and kinds == sorted(want_positions)
    verdict = "PASS" if ok else "FAIL"
    ml = resp.get("ml", {})
    print(
        f"[{verdict}] {name}: http={resp_status} status={resp['status']} "
        f"summary={ml.get('action_summary')} err={ml.get('error')} positions={pos}"
    )
    return ok


def run(case):
    if case == "case1":
        s, r = ml_request("buy")
        return check("1 shadow-mode buy passthrough", "accepted", s, r, ["BUY"])
    if case == "case2":
        s, r = ml_request("buy", current_position="LONG")
        return check("2 same-direction ignore (enforce)", "skipped", s, r, ["BUY"])
    if case == "case3":
        s, r = ml_request("sell", current_position="LONG")
        return check(
            "3 FLIP_SHORT (enforce, low threshold)", "accepted", s, r, ["SELL"]
        )
    if case == "case4":
        s, r = ml_request("buy", current_position="SHORT")
        return check("4 CLOSE_ONLY (enforce, high threshold)", "accepted", s, r, [])
    if case == "case5":
        s, r = ml_request("buy")
        return check("5 enforce-mode skip (NOTHING)", "skipped", s, r, [])
    if case == "case6":
        s, r = ml_request("buy")
        return check("6 fail-open (predictor down)", "accepted", s, r, ["BUY"])
    if case == "case7":
        s, r = flat_request("closelong")
        return check("7 flat-path closelong", "accepted", s, r, [])
    print(f"unknown case: {case}")
    return False


if __name__ == "__main__":
    sys.exit(0 if run(sys.argv[1]) else 1)
