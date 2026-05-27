CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS roles (
    id SMALLSERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE CHECK (name IN ('user', 'support', 'super_admin'))
);

INSERT INTO roles (name)
VALUES ('user'), ('support'), ('super_admin')
ON CONFLICT (name) DO NOTHING;

CREATE TABLE IF NOT EXISTS user_roles (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_id SMALLINT NOT NULL REFERENCES roles(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, role_id)
);

CREATE TABLE IF NOT EXISTS plan_tiers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,
    max_instances INTEGER NOT NULL CHECK (max_instances >= 0),
    max_concurrent_connections INTEGER NOT NULL CHECK (max_concurrent_connections >= 0),
    max_signals_per_day INTEGER NOT NULL CHECK (max_signals_per_day >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_limit_overrides (
    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    max_instances INTEGER CHECK (max_instances IS NULL OR max_instances >= 0),
    max_concurrent_connections INTEGER CHECK (max_concurrent_connections IS NULL OR max_concurrent_connections >= 0),
    max_signals_per_day INTEGER CHECK (max_signals_per_day IS NULL OR max_signals_per_day >= 0),
    reason TEXT NOT NULL,
    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS admin_audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    target_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    before_state JSONB,
    after_state JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS licenses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    license_key TEXT NOT NULL UNIQUE,
    hmac_secret TEXT NOT NULL,
    plan_tier_id UUID REFERENCES plan_tiers(id) ON DELETE RESTRICT,
    active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS instances (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    license_id UUID NOT NULL REFERENCES licenses(id) ON DELETE CASCADE,
    instance_key TEXT NOT NULL,
    platform TEXT NOT NULL CHECK (platform IN ('mt4', 'mt5', 'dxtrade')),
    active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (license_id, instance_key)
);

CREATE TABLE IF NOT EXISTS regions (
    id TEXT PRIMARY KEY,
    cloud TEXT NOT NULL,
    city TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS instance_region_pref (
    instance_id UUID PRIMARY KEY REFERENCES instances(id) ON DELETE CASCADE,
    preferred_region_id TEXT NOT NULL REFERENCES regions(id) ON DELETE RESTRICT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS accepted_signals (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    license_id UUID NOT NULL REFERENCES licenses(id) ON DELETE CASCADE,
    instance_id UUID REFERENCES instances(id) ON DELETE SET NULL,
    trace_id TEXT NOT NULL,
    ingress_region TEXT NOT NULL,
    bridge_region TEXT,
    command TEXT NOT NULL,
    symbol TEXT NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (id, received_at)
);

SELECT create_hypertable('accepted_signals', 'received_at', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS audit_rejections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rejected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    trace_id TEXT,
    license_id TEXT,
    reason_code TEXT NOT NULL,
    ingress_region TEXT NOT NULL,
    payload_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fills (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    signal_id UUID,
    license_id UUID NOT NULL REFERENCES licenses(id) ON DELETE CASCADE,
    instance_id UUID REFERENCES instances(id) ON DELETE SET NULL,
    trace_id TEXT NOT NULL,
    broker_order_id TEXT,
    status TEXT NOT NULL,
    error_code TEXT,
    error_message TEXT,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_type TEXT NOT NULL,
    status TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS notifications_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    channel TEXT NOT NULL,
    template TEXT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ea_connection_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    license_id UUID NOT NULL REFERENCES licenses(id) ON DELETE CASCADE,
    instance_id UUID REFERENCES instances(id) ON DELETE SET NULL,
    account_number TEXT NOT NULL,
    broker_name TEXT NOT NULL,
    account_type TEXT NOT NULL,
    platform TEXT NOT NULL CHECK (platform IN ('mt4', 'mt5', 'dxtrade')),
    ea_version TEXT NOT NULL,
    bridge_region TEXT NOT NULL,
    connected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    disconnected_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS system_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trace_id TEXT,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS report_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    report_type TEXT NOT NULL,
    data_as_of TIMESTAMPTZ NOT NULL,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (report_type, data_as_of, content_hash)
);

CREATE TABLE IF NOT EXISTS report_findings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    report_run_id UUID NOT NULL REFERENCES report_runs(id) ON DELETE CASCADE,
    model_run_id TEXT NOT NULL,
    finding_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS report_subscriptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    report_type TEXT NOT NULL,
    schedule TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS model_registry (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    model_name TEXT NOT NULL,
    model_version TEXT NOT NULL,
    model_run_id TEXT NOT NULL UNIQUE,
    artifact_uri TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS signal_fingerprints (
    license_id UUID NOT NULL REFERENCES licenses(id) ON DELETE CASCADE,
    body_sha256 TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (license_id, body_sha256)
);

CREATE INDEX IF NOT EXISTS idx_accepted_signals_license_time
    ON accepted_signals (license_id, received_at DESC);

CREATE INDEX IF NOT EXISTS idx_accepted_signals_trace_id
    ON accepted_signals (trace_id);

CREATE INDEX IF NOT EXISTS idx_fills_trace_id
    ON fills (trace_id);

CREATE INDEX IF NOT EXISTS idx_fills_status_created
    ON fills (status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_ea_sessions_instance_open
    ON ea_connection_sessions (instance_id, connected_at DESC)
    WHERE disconnected_at IS NULL;

CREATE OR REPLACE FUNCTION prevent_admin_audit_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'admin_audit_log is append-only';
END;
$$;

DROP TRIGGER IF EXISTS admin_audit_log_append_only_update ON admin_audit_log;
CREATE TRIGGER admin_audit_log_append_only_update
BEFORE UPDATE ON admin_audit_log
FOR EACH ROW EXECUTE FUNCTION prevent_admin_audit_mutation();

DROP TRIGGER IF EXISTS admin_audit_log_append_only_delete ON admin_audit_log;
CREATE TRIGGER admin_audit_log_append_only_delete
BEFORE DELETE ON admin_audit_log
FOR EACH ROW EXECUTE FUNCTION prevent_admin_audit_mutation();
