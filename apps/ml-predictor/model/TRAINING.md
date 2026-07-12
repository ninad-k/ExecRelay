# Model provenance — `xgb_production.json`

This document records what is **verifiable by inspecting the shipped
artifact and this repository** as of the current commit. It intentionally
does **not** claim anything about how the model was trained beyond what the
artifact itself proves. See "Unknown / to be documented by model owner"
below for the gaps.

## Artifact identity

| File | SHA-256 | Size |
|---|---|---|
| `xgb_production.json` | `6bd309cb2fe090300c54c6e97c5731bfb03275edaf45960b60cac4d080cd7ed5` | 924,329 bytes |
| `feature_order.txt` | `ab80461fb7b92dcfd5abc2f0692ea164ed23fa64ac1283ca374503ad3e619640` | 340 bytes |

Both checksums are pinned in [`SHA256SUMS`](SHA256SUMS) (standard `sha256sum`
format). The Dockerfile verifies them at image-build time
(`sha256sum -c SHA256SUMS` after `COPY model/`), and
`tests/test_checksums.py` recomputes and matches them so a model swap
without an updated `SHA256SUMS` fails `pytest` locally too.

At service startup, `XGBPredictor` independently hashes the model file and
exposes the first 12 hex characters as `model_version` — in the `/predict`
response body, in startup logs, and as the `version` label on the
`ml_model_info` Prometheus gauge. For the artifact above, that is:

```
model_version = 6bd309cb2fe0
```

Any artifact swap (intentional or not) changes this value, which is how a
version rollout or an accidental artifact drift shows up in monitoring
without anyone needing to compare checksums by hand.

## What's recorded inside the model JSON

XGBoost's JSON model format embeds enough of its own metadata to answer
these questions directly from the file (parsed via `json.load` on
`xgb_production.json`):

| Attribute | Value | Where in the JSON |
|---|---|---|
| Model serialization ("JSON schema") version | `[3, 2, 0]` | top-level `version` |
| Booster type | `gbtree` | `learner.gradient_booster.name` |
| Objective | `binary:logistic` | `learner.objective.name` |
| `scale_pos_weight` | `1` | `learner.objective.reg_loss_param.scale_pos_weight` |
| Number of input features | `36` | `learner.learner_model_param.num_feature` |
| `base_score` | `4.2058823E-1` | `learner.learner_model_param.base_score` |
| Number of trees stored | `304` | `learner.gradient_booster.model.gbtree_model_param.num_trees` |
| `num_parallel_tree` | `1` (no boosted-forest averaging) | same `gbtree_model_param` |
| `best_iteration` (early-stopping checkpoint) | `253` | `learner.attributes.best_iteration` |
| `best_score` (metric at that checkpoint) | `0.5584990731378902` | `learner.attributes.best_score` |
| Estimator wrapper | `scikit_learn` attribute records `{"_estimator_type": "classifier"}` | `learner.attributes.scikit_learn` |

Notes on reading these:

- `best_iteration=253` with `304` trees stored means the model was trained
  with early stopping (via the scikit-learn `XGBClassifier` wrapper, per the
  `scikit_learn` attribute) and the training run continued past the best
  checkpoint before stopping — the JSON does not by itself say whether
  inference here uses all 304 trees or is limited to the first 254
  (`best_iteration + 1`). `XGBPredictor` calls `Booster.predict()` with no
  `iteration_range`, so **all 304 stored trees are used** at inference time
  in this service, not just the best-iteration subset.
- Maximum tree depth is **not** stored as a hyperparameter in the JSON (only
  the resulting tree structures are). Walking `left_children`/`right_children`
  for every stored tree gives an *observed* structural maximum depth of
  **6** across all 304 trees — this is a fact about the shipped trees, not a
  confirmed `max_depth` training hyperparameter (a hyperparameter search
  could have settled on a larger cap that just wasn't reached by every tree).
- `learner.feature_names` and `learner.feature_types` are empty in this
  artifact — the model was saved via `Booster.save_model()` without feature
  metadata attached. That's why `model/feature_order.txt` exists: it is the
  **only** source of feature-name-to-column mapping, and it must travel with
  this exact artifact.

## The 36-feature contract

`XGBPredictor` builds its input row strictly in the order given by
[`feature_order.txt`](feature_order.txt): `direction` first, followed by 35
model features. `direction` (`+1` for a long/buy signal, `-1` for a
short/sell signal) is injected server-side; it is never sent by the
upstream caller.

The other 35 features are computed upstream, on TradingView, by
[`pine/Combo_Webhook_Pine.pine`](../../pine/Combo_Webhook_Pine.pine) and
shipped in the webhook alert's `features` object — see
[`pine/README.md`](../../pine/README.md) for the full payload contract and
field reference. `apps/ml-predictor/tests/test_contract.py` enforces, via a
golden fixture, that the alert payload's feature keys are exactly
`feature_order.txt` minus `direction` — if the Pine script's feature set and
this file drift apart, that test fails.

`XGBPredictor.__init__` also hard-fails (`ValueError`) if
`feature_order.txt` does not contain exactly 36 entries
(`EXPECTED_FEATURE_COUNT` in `xgb_predictor.py`), as a second guard against a
truncated or mismatched feature list shipping alongside the model.

## How inference consumes the artifact

`xgb_predictor.py`'s `XGBPredictor`:

1. Reads the raw bytes of `xgb_production.json` once (for both the
   `model_version` hash and to load the model), and loads it into an
   `xgboost.Booster` via `Booster.load_model()` — **not** the scikit-learn
   `XGBClassifier` wrapper, even though the artifact's `scikit_learn`
   attribute shows it was originally trained through that wrapper.
2. Builds a single-row feature vector per request, ordered per
   `feature_order.txt`, and wraps it in an `xgb.DMatrix` with
   `feature_names=feature_order` (compensating for the artifact having no
   embedded feature names).
3. Calls `Booster.predict()` on that `DMatrix`. Because the objective is
   `binary:logistic`, this returns the positive-class probability
   (`P(win)`) directly — no separate sigmoid/link-function step is needed
   in this code.
4. Compares `P(win)` against a threshold, `ML_THRESHOLD` (env var, default
   **`0.50`**, see `THRESHOLD` in `app.py`), to decide whether the
   "Option 1" open/close/flip filter passes. This threshold is an inference
   time decision knob, independent of anything stored in the model artifact
   itself.

## Unknown / to be documented by model owner

The following are **not recorded anywhere in this repository or the
artifact** and must not be inferred or guessed. Anyone relying on this
model for risk/compliance sign-off needs these supplied directly by whoever
trained it:

- Training data window (symbols, date range, bar timeframe, data source).
- Labeling methodology (how "win" vs "loss" was defined — e.g. barrier
  method, fixed horizon, take-profit/stop-loss levels used to generate
  labels).
- Train/validation/test split methodology (time-based vs random, walk-forward
  or otherwise) and how leakage was avoided given serially correlated
  price data.
- Hyperparameter search process (search space, method, number of trials) that
  produced this specific tree ensemble — only the *result* (tree structures,
  objective, `scale_pos_weight`, `base_score`) is recoverable from the JSON,
  not the search that led to it.
- Evaluation metrics beyond the single `best_score` value stored in the
  artifact's `attributes` (e.g. no recorded precision/recall, Sharpe,
  drawdown, or out-of-sample backtest results tied to this artifact).
- Training/build environment (xgboost training-time package version,
  hardware, random seed) — the `version: [3, 2, 0]` field in the JSON is the
  model's internal serialization format version, not necessarily the
  `xgboost` package version used during training.
- Date the artifact was produced and who produced it.

If you are the model owner, please fill in the above (ideally as a
follow-up section in this file, or a linked training-run record) rather
than leaving downstream consumers to guess.
