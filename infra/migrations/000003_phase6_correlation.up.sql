-- Phase 6: Signal Correlation & Risk Management Schema

-- Signal grouping for multi-signal strategies
CREATE TABLE IF NOT EXISTS signal_groups (
    id BIGSERIAL PRIMARY KEY,
    license_id UUID NOT NULL REFERENCES licenses(id) ON DELETE CASCADE,
    created_at TIMESTAMP DEFAULT now(),
    group_name VARCHAR(255),
    metadata JSONB DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_signal_groups_license ON signal_groups(license_id);

-- Track which signals belong to which groups
CREATE TABLE IF NOT EXISTS signal_group_members (
    id BIGSERIAL PRIMARY KEY,
    group_id BIGINT NOT NULL REFERENCES signal_groups(id) ON DELETE CASCADE,
    signal_id UUID NOT NULL,
    membership_reason VARCHAR(255),
    added_at TIMESTAMP DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_group_members_group ON signal_group_members(group_id);
CREATE INDEX IF NOT EXISTS idx_group_members_signal ON signal_group_members(signal_id);

-- Pre-computed symbol correlations for portfolio analysis
CREATE TABLE IF NOT EXISTS symbol_correlations (
    id BIGSERIAL PRIMARY KEY,
    license_id UUID NOT NULL REFERENCES licenses(id) ON DELETE CASCADE,
    symbol_a VARCHAR(20) NOT NULL,
    symbol_b VARCHAR(20) NOT NULL,
    correlation_coefficient FLOAT NOT NULL CHECK (correlation_coefficient >= -1.0 AND correlation_coefficient <= 1.0),
    calculated_at TIMESTAMP DEFAULT now(),
    lookback_days INTEGER DEFAULT 30,
    UNIQUE(license_id, symbol_a, symbol_b)
);
CREATE INDEX IF NOT EXISTS idx_correlations_license ON symbol_correlations(license_id);
CREATE INDEX IF NOT EXISTS idx_correlations_symbols ON symbol_correlations(symbol_a, symbol_b);

-- Portfolio exposure limits per account
CREATE TABLE IF NOT EXISTS portfolio_exposure_limits (
    id BIGSERIAL PRIMARY KEY,
    license_id UUID NOT NULL REFERENCES licenses(id) ON DELETE CASCADE,
    account_id VARCHAR(100) NOT NULL,
    max_notional_usd DECIMAL(12,2),
    max_position_size_pct DECIMAL(5,2),
    max_loss_pct DECIMAL(5,2),
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now(),
    UNIQUE(license_id, account_id)
);
CREATE INDEX IF NOT EXISTS idx_limits_license ON portfolio_exposure_limits(license_id);
CREATE INDEX IF NOT EXISTS idx_limits_account ON portfolio_exposure_limits(account_id);

-- Current account positions snapshot
CREATE TABLE IF NOT EXISTS account_positions (
    id BIGSERIAL PRIMARY KEY,
    license_id UUID NOT NULL REFERENCES licenses(id) ON DELETE CASCADE,
    account_id VARCHAR(100) NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    position_size DECIMAL(12,8) NOT NULL,
    entry_price DECIMAL(12,8),
    current_price DECIMAL(12,8),
    updated_at TIMESTAMP DEFAULT now(),
    UNIQUE(license_id, account_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_positions_license ON account_positions(license_id);
CREATE INDEX IF NOT EXISTS idx_positions_account ON account_positions(account_id);
CREATE INDEX IF NOT EXISTS idx_positions_symbol ON account_positions(symbol);

-- Account drawdown tracking
CREATE TABLE IF NOT EXISTS account_drawdowns (
    id BIGSERIAL PRIMARY KEY,
    license_id UUID NOT NULL REFERENCES licenses(id) ON DELETE CASCADE,
    account_id VARCHAR(100) NOT NULL,
    peak_equity DECIMAL(12,2) NOT NULL,
    current_equity DECIMAL(12,2) NOT NULL,
    drawdown_pct DECIMAL(5,2) NOT NULL,
    recorded_at TIMESTAMP DEFAULT now(),
    UNIQUE(license_id, account_id)
);
CREATE INDEX IF NOT EXISTS idx_drawdowns_license ON account_drawdowns(license_id);

-- Signal features for ML model training
CREATE TABLE IF NOT EXISTS signal_features (
    id BIGSERIAL PRIMARY KEY,
    signal_id UUID NOT NULL,
    license_id UUID NOT NULL REFERENCES licenses(id) ON DELETE CASCADE,
    symbol VARCHAR(20),
    time_of_day_hour INTEGER,
    day_of_week INTEGER,
    symbol_volatility FLOAT,
    signal_frequency INTEGER,
    win_rate_pct FLOAT,
    account_drawdown_pct FLOAT,
    portfolio_correlation_exposure FLOAT,
    created_at TIMESTAMP DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_features_signal ON signal_features(signal_id);
CREATE INDEX IF NOT EXISTS idx_features_license ON signal_features(license_id);
CREATE INDEX IF NOT EXISTS idx_features_symbol ON signal_features(symbol);

-- ML model versions and metadata
CREATE TABLE IF NOT EXISTS ml_models (
    id BIGSERIAL PRIMARY KEY,
    license_id UUID REFERENCES licenses(id) ON DELETE CASCADE,
    model_type VARCHAR(50) NOT NULL DEFAULT 'signal_success_predictor',
    model_version VARCHAR(50) NOT NULL,
    training_date TIMESTAMP DEFAULT now(),
    metrics JSONB DEFAULT '{}',
    model_path VARCHAR(500),
    is_active BOOLEAN DEFAULT false
);
CREATE INDEX IF NOT EXISTS idx_models_license ON ml_models(license_id);
CREATE INDEX IF NOT EXISTS idx_models_type ON ml_models(model_type);

-- Backtesting results and performance metrics
CREATE TABLE IF NOT EXISTS backtesting_results (
    id BIGSERIAL PRIMARY KEY,
    backtest_job_id UUID,
    license_id UUID NOT NULL REFERENCES licenses(id) ON DELETE CASCADE,
    date_range_start DATE,
    date_range_end DATE,
    total_signals INTEGER,
    total_fills INTEGER,
    fill_rate_pct DECIMAL(5,2),
    gross_pnl DECIMAL(12,2),
    net_pnl DECIMAL(12,2),
    sharpe_ratio FLOAT,
    max_drawdown_pct DECIMAL(5,2),
    win_count INTEGER,
    loss_count INTEGER,
    win_pct DECIMAL(5,2),
    avg_win_pnl DECIMAL(12,2),
    avg_loss_pnl DECIMAL(12,2),
    payload JSONB DEFAULT '{}',
    created_at TIMESTAMP DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_backtest_license ON backtesting_results(license_id);
CREATE INDEX IF NOT EXISTS idx_backtest_job ON backtesting_results(backtest_job_id);

-- Risk limit breach audit trail
CREATE TABLE IF NOT EXISTS risk_breach_log (
    id BIGSERIAL PRIMARY KEY,
    license_id UUID NOT NULL REFERENCES licenses(id) ON DELETE CASCADE,
    account_id VARCHAR(100),
    breach_type VARCHAR(50),
    current_value DECIMAL(12,2),
    limit_value DECIMAL(12,2),
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_breach_license ON risk_breach_log(license_id);
CREATE INDEX IF NOT EXISTS idx_breach_account ON risk_breach_log(account_id);
CREATE INDEX IF NOT EXISTS idx_breach_type ON risk_breach_log(breach_type);
CREATE INDEX IF NOT EXISTS idx_breach_date ON risk_breach_log(created_at);
