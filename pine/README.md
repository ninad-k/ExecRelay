# Pine — TradingView signal source + feature generator

This directory holds the TradingView Pine scripts that act as the **upstream
half of the ML execution path**. They generate entry signals *and* compute the
feature vector the [`ml-predictor`](../apps/ml-predictor) service scores.

| Script | What it does |
|---|---|
| [`Combo_Webhook_Pine.pine`](Combo_Webhook_Pine.pine) | AlgoAlpha SuperTrend + EMA50 + MACD + DMI entry logic. On a BUY/SELL entry it emits a webhook alert containing the order fields **and** all 35 model features. |

## Why this lives here

The features the XGBoost model needs (`ret_*`, `dist_ema_*`, `rsi_14`, `adx_14`,
`bb_z`, …) are **not computed server-side** — they are computed on TradingView,
on the same bar that fires the signal, and shipped inside the alert JSON. This
script is therefore the canonical source of those features. Versioning it here
keeps it in lockstep with [`apps/ml-predictor/model/feature_order.txt`](../apps/ml-predictor/model/feature_order.txt):
if the model's feature set changes, this script and that file must change together.

## Webhook payload contract

A BUY entry produces (a SELL entry is identical with `action:"sell"`):

```json
{
  "x_account": "default",
  "action": "buy",
  "symbol": "BTCUSD",
  "comment": "AlgoCombo",
  "volume": 0.1,
  "use_xgb": true,
  "features": {
    "ret_1": 0.00012, "ret_3": 0.0003, "ret_12": 0.0009, "ret_36": 0.001,
    "ret_72": 0.002, "ret_288": 0.004, "range_pct": 0.001, "body_pct": 0.0004,
    "upper_wick": 0.0002, "lower_wick": 0.0001, "atr_pct": 0.0009,
    "dist_ema_9": 0.0001, "dist_ema_21": 0.0003, "dist_ema_50": 0.0006,
    "dist_ema_200": 0.002, "ema_50_slope": 0.0001, "ema_200_slope": 0.00005,
    "rsi_14": 58.2, "plus_di": 27.1, "minus_di": 14.3, "adx_14": 31.0,
    "bb_z": 0.92, "bb_width": 0.013, "vol_72": 0.0011, "vol_288": 0.0015,
    "active_bar": 1, "active_rate_288": 0.97, "h1_dist_ema50": 0.003,
    "h1_ret_24": 0.006, "h4_dist_ema50": 0.008, "hour_utc": 14, "dow": 2,
    "month": 6, "minute_of_day": 870, "session": 2
  }
}
```

### Field reference

| Field | Type | Notes |
|---|---|---|
| `x_account` | string | Account / instance identifier |
| `action` | `"buy"` \| `"sell"` | Maps to predictor `direction`: buy → `1`, sell → `-1` |
| `symbol` | string | TradingView ticker (server applies broker symbol mapping) |
| `comment` | string | Strategy tag — isolates strategies sharing a symbol/account |
| `volume` | number | Lots |
| `use_xgb` | bool | If `false`, the signal bypasses the ML filter entirely |
| `features` | object | The 35 model features below |

### The 35 features

These are exactly the entries of
[`feature_order.txt`](../apps/ml-predictor/model/feature_order.txt) **minus
`direction`**. `direction` is the 36th model input and is injected server-side
from `action` (buy → `1`, sell → `-1`) — the Pine script does **not** send it.

```
ret_1 ret_3 ret_12 ret_36 ret_72 ret_288
range_pct body_pct upper_wick lower_wick atr_pct vol_72 vol_288
dist_ema_9 dist_ema_21 dist_ema_50 ema_50_slope dist_ema_200 ema_200_slope
rsi_14 plus_di minus_di adx_14 bb_z bb_width
active_bar active_rate_288
h1_dist_ema50 h1_ret_24 h4_dist_ema50
hour_utc dow month minute_of_day session
```

> The model requires **all 35** features to be present. A missing key makes the
> predictor return `error: "Missing features: [...]"` and take no action. `dow`
> is the Python `weekday()` convention (Mon=0 … Sun=6); the script converts from
> Pine's `dayofweek` accordingly.

## How it connects to ml-predictor

```
[TradingView: Combo_Webhook_Pine.pine]   computes features on the signal bar
        │  alert JSON (above)
        ▼
[ingress]        parses the alert, carries `features` through to the signal
        ▼
[ml-predictor]   injects direction, scores the 36-feature vector,
                 returns OPEN / FLIP / CLOSE_ONLY / NOTHING (Option-1 logic)
```

The ingress-side passthrough of the `features` object is what wires this path
end-to-end. See [`apps/ml-predictor`](../apps/ml-predictor) for the scoring side.

## Setting up the alert on TradingView

1. Add the indicator to a chart on the timeframe the model was trained for.
2. Set the **Webhook** inputs: `x_account`, strategy tag, volume, and `use_xgb`.
3. Create an alert on the "BUY → webhook" / "SELL → webhook" conditions with the
   ExecRelay ingress webhook URL as the alert's webhook target.
