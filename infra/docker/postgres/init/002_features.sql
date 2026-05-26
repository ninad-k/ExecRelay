-- Signal replay: store raw proto bytes for re-publish
ALTER TABLE accepted_signals ADD COLUMN IF NOT EXISTS raw_payload BYTEA;

-- HMAC secret rotation: dual-secret window
ALTER TABLE licenses ADD COLUMN IF NOT EXISTS pending_hmac_secret TEXT;

-- Plan enforcement: per-license daily signal count cache
CREATE TABLE IF NOT EXISTS daily_signal_counts (
    license_id UUID NOT NULL REFERENCES licenses(id) ON DELETE CASCADE,
    count_date DATE NOT NULL DEFAULT CURRENT_DATE,
    signal_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (license_id, count_date)
);
