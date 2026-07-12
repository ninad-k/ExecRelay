# 8. Opt-in JSON `/webhook/ml` path for ML-filtered signals

Date: 2026-06-23
Status: Proposed

## Context

The `ml-predictor` service now loads a trained XGBoost model and applies
"Option 1" open/close/flip guidance over a 36-feature vector (see
[`apps/ml-predictor`](../../apps/ml-predictor) and
[`pine/`](../../pine)). The 35 input features are computed on TradingView by a
Pine indicator and shipped in the webhook alert as a **nested JSON object**:

```json
{"x_account":"...","action":"buy","use_xgb":true,
 "features":{"ret_1":...,"dist_ema_50":...,"rsi_14":...,"adx_14":..., ...}}
```

To act on the model, ingress must get that `features` object to the predictor,
score it, and turn the decision into a published signal. Two facts make this
non-trivial:

1. **The ingress wire format is not JSON.** `parser.Parse` reads a flat,
   comma-separated, strictly-allowlisted `license_id, command, symbol, key=value`
   string and rejects unknown params (`ErrUnknownParam`). It is hand-rolled for
   the documented same-region p99 95 ms hot path (see ADR 0004). A nested
   35-key object has no representation in that scheme, and JSON commas would
   break field-splitting.

2. **A vestigial ML call used to exist on the hot path.** `scoreSignalWithML`
   ([`risk.go`](../../apps/ingress/internal/ingress/risk.go)) POSTed 7
   hardcoded placeholder features to `ml-predictor:8080/predict`, expected
   `{confidence}`, and **never used the result for gating** — it was only
   logged and echoed as `ml_confidence` in the response. The XGBoost
   transplant changed `/predict`'s contract (7-field payload now 400s, and
   the response has no `confidence` field), so this call mismatched and was
   polluting `ml_prediction_errors_total` on every flat webhook. **This has
   already been removed** (ahead of `/webhook/ml` below landing) — see
   Decision.

### Options considered for carrying features

- **Extend the flat parser** with 35 `f_*=` params + carry them on `Signal`,
  and rewrite the Pine script to the flat format. Keeps one path but bloats the
  strict hot-path vocabulary (fits `MaxParams=48`, barely) and abandons the
  Pine JSON contract.
- **Single `features=<base64(json)>` param** on the flat format. Minimal parser
  delta, but an encoding hack plus a Pine rewrite.
- **A separate opt-in JSON path.** Isolates JSON parsing + ML to a dedicated
  route; the flat hot path and its 95 ms budget are untouched.

## Decision

Add a dedicated **`POST /webhook/ml`** endpoint that accepts a JSON body, scores
it via the predictor's decision API, and publishes the resulting command through
the existing `signalProto` + NATS path. The flat `/webhook` path is unchanged.

**Request body** (ExecRelay-native — uses `license_id`/`secret`, not Pine's
`x_account`; a thin TradingView-side adapter or an updated Pine template
supplies these):

```json
{
  "license_id": "...", "secret": "...",
  "action": "buy" | "sell", "symbol": "BTCUSD",
  "volume": 0.1, "sl": 0, "tp": 0, "comment": "AlgoCombo",
  "current_position": "LONG" | "SHORT" | null,
  "features": { "...35 features per feature_order.txt minus `direction`..." }
}
```

**Shared preamble.** `/webhook/ml` reuses the exact gating + auth chain from
`/webhook`: perimeter token, kill-switch, per-IP rate limit, CIDR allowlist,
timestamp window, license lookup, `secret`, HMAC-over-raw-body, daily quota, and
exposure limits. (Implementation refactors that preamble into a shared helper
rather than duplicating it.)

**Scoring.** `direction` is derived from `action` (buy → `1`, sell → `-1`) and
sent with `features` and `current_position` to the predictor.

**Decision → command mapping.** ExecRelay's existing command vocabulary already
expresses Option-1 semantics exactly, so the translation is faithful:

| Predictor `action_summary` | ExecRelay command | Published? |
|---|---|---|
| `OPEN_LONG` | `buy` | yes |
| `OPEN_SHORT` | `sell` | yes |
| `FLIP_LONG` | `closeshortopenlong` | yes |
| `FLIP_SHORT` | `closelongopenshort` | yes |
| `CLOSE_ONLY` (signal SHORT, was LONG) | `closelong` | yes |
| `CLOSE_ONLY` (signal LONG, was SHORT) | `closeshort` | yes |
| `NOTHING` | — | no → `200 {"status":"skipped"}` |

The published `Signal` carries `symbol`, the mapped command, and
`volume`/`sl`/`tp`/`comment` as params, on the same
`signals.<platform>.<license>.<instance>` subject as the flat path.

**`current_position` is caller-declared** in v1. Photos tracked it in-memory;
ExecRelay holds that the EA is the execution authority, so server-side sourcing
of position state (from bridge/EA reports or `account_positions`) is deferred to
a follow-up — see Notes.

**Retire the vestigial `scoreSignalWithML`.** *(Already landed, ahead of
`/webhook/ml` itself — see Status.)* The placeholder 7-feature call, the
`MLPredictRequest`/`MLPredictResponse` types, and the `ml_confidence` response
field have been removed from the flat path. It never gated anything and had
started mismatching the predictor contract; removing it also drops a blocking
HTTP call from the hot path, consistent with the 95 ms / no-DB-on-hot-path
posture.

## Consequences

**Positive**

- The flat hot path and its latency budget are untouched; JSON parsing and the
  synchronous predictor call live only on the opt-in route.
- Decision→command mapping reuses existing, tested commands (incl. the compound
  `closelongopenshort` / `closeshortopenlong` flips) — no new wire semantics.
- One ML contract instead of two; the dead placeholder scoring is removed.

**Negative**

- A second ingestion route to maintain, with its own auth wiring (mitigated by
  sharing the preamble helper).
- `/webhook/ml` makes a **synchronous** predictor call, so its latency profile is
  deliberately looser than the flat path's 95 ms target. Callers opt into that.
- Removing `ml_confidence` from the flat response (already done) was a
  (minor) response-shape change for any client reading it.
- The TradingView side must send `license_id`/`secret` (adapter or updated Pine),
  rather than the legacy `x_account` shape.

## Notes / follow-ups

- **EA-sourced `current_position`.** Replace the caller-declared field by
  resolving open position state from the EA/bridge fill stream or
  `account_positions`, so the Option-1 close/flip logic is authoritative.
- **Coverage.** New endpoint needs Go tests to hold the 80% gate on shared
  packages: auth-reuse paths, each `action_summary` → command branch, the
  `NOTHING` skip, malformed-JSON and missing-features rejections, and the
  predictor-down fallback.
- This ADR is **Proposed**; it flips to **Accepted** when `/webhook/ml` lands.
  Implementation was designed but not built in the originating session because no
  Go toolchain was available there to compile/test it.
