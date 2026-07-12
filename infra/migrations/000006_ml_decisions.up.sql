-- 000006_ml_decisions — audit trail for ADR 0008's opt-in JSON
-- POST /webhook/ml path. One row per /webhook/ml request: which command the
-- XGBoost predictor recommended, whether it was actually published (shadow
-- vs. enforce mode), and where current_position came from. Lets an operator
-- answer "what would the model have done?" during the shadow-mode rollout,
-- and "what did the model do?" once ML_ENFORCE=true.

CREATE TABLE IF NOT EXISTS ml_decisions (
    id                 BIGSERIAL PRIMARY KEY,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    trace_id           VARCHAR(64) NOT NULL,
    license_id         VARCHAR(64) NOT NULL,
    symbol             VARCHAR(20) NOT NULL,
    action             VARCHAR(8)  NOT NULL,   -- "buy" | "sell" (the caller's original action)
    prob_win           DOUBLE PRECISION,        -- null when the predictor call failed
    threshold          DOUBLE PRECISION NOT NULL DEFAULT 0,
    action_summary     VARCHAR(16) NOT NULL,    -- OPEN_LONG | OPEN_SHORT | FLIP_LONG | FLIP_SHORT | CLOSE_ONLY | NOTHING
    published_command  TEXT,                    -- null when nothing was published (skipped)
    enforced           BOOLEAN NOT NULL,        -- ML_ENFORCE at request time
    model_version      TEXT,
    position_source    VARCHAR(16) NOT NULL,    -- caller | db | unknown
    error              TEXT                     -- predictor error, if any (fail-open path)
);

CREATE INDEX IF NOT EXISTS idx_ml_decisions_trace_id   ON ml_decisions(trace_id);
CREATE INDEX IF NOT EXISTS idx_ml_decisions_license    ON ml_decisions(license_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ml_decisions_created    ON ml_decisions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ml_decisions_summary    ON ml_decisions(action_summary);
