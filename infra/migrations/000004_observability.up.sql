-- 000004_observability — request log + dead-letter table for "troubleshoot
-- every request" requirement. request_log captures EVERY ingress webhook
-- attempt (accept or reject) so an operator can answer "what happened to
-- this call?" from a single row. dead_letter_messages parks malformed NATS
-- payloads so they don't get silently dropped.

-- Every ingress webhook decision (accept/reject) lands here with full
-- context: request_id, trace_id, license, latency, outcome, reason.
-- Joined with accepted_signals + fills + events for a complete trace.
CREATE TABLE IF NOT EXISTS request_log (
    id            BIGSERIAL PRIMARY KEY,
    received_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    request_id    VARCHAR(64) NOT NULL,
    trace_id      VARCHAR(64),
    service       VARCHAR(32) NOT NULL,
    route         VARCHAR(128) NOT NULL,
    method        VARCHAR(8)  NOT NULL,
    client_ip     INET,
    license_key   VARCHAR(64),
    status_code   INT NOT NULL,
    outcome       VARCHAR(32) NOT NULL,   -- accepted | rejected | error
    reason_code   VARCHAR(64),            -- rejection/error code
    latency_ms    INT NOT NULL,
    body_sha256   CHAR(64),
    user_agent    VARCHAR(256),
    detail        JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_request_log_request_id ON request_log(request_id);
CREATE INDEX IF NOT EXISTS idx_request_log_trace_id   ON request_log(trace_id);
CREATE INDEX IF NOT EXISTS idx_request_log_license    ON request_log(license_key, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_request_log_received   ON request_log(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_request_log_outcome    ON request_log(outcome, received_at DESC);

-- Malformed messages that couldn't be parsed by persist or any other consumer.
-- Operators inspect this table to see what came in, decide whether to fix
-- producer or write a one-off replay.
CREATE TABLE IF NOT EXISTS dead_letter_messages (
    id            BIGSERIAL PRIMARY KEY,
    received_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    subject       VARCHAR(128) NOT NULL,
    reason        VARCHAR(64)  NOT NULL,
    error_detail  TEXT,
    payload       BYTEA NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dead_letter_received ON dead_letter_messages(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_dead_letter_subject  ON dead_letter_messages(subject);
CREATE INDEX IF NOT EXISTS idx_dead_letter_reason   ON dead_letter_messages(reason);
