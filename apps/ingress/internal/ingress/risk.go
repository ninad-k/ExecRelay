package ingress

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
)

type MLPredictRequest struct {
	TimeOfDayHour                int     `json:"time_of_day_hour"`
	DayOfWeek                    int     `json:"day_of_week"`
	SymbolVolatility             float64 `json:"symbol_volatility"`
	SignalFrequency              float64 `json:"signal_frequency"`
	WinRatePct                   float64 `json:"win_rate_pct"`
	AccountDrawdownPct           float64 `json:"account_drawdown_pct"`
	PortfolioCorrelationExposure float64 `json:"portfolio_correlation_exposure"`
}

type MLPredictResponse struct {
	Confidence float64 `json:"confidence"`
}

type ExposureCheckResult struct {
	AllowedToProceed bool
	CurrentExposure  float64
	ExposureLimit    float64
	Reason           string
}

func (h *Handler) scoreSignalWithML(ctx context.Context, symbol string, timeReceived int64) (float64, error) {
	// Extract features from signal
	now := h.now()
	req := MLPredictRequest{
		TimeOfDayHour:                now.Hour(),
		DayOfWeek:                    int(now.Weekday()),
		SymbolVolatility:             0.025, // default 2.5% volatility
		SignalFrequency:              1.0,   // default 1 signal/day
		WinRatePct:                   50.0,  // default 50% win rate
		AccountDrawdownPct:           0.0,   // default 0% drawdown
		PortfolioCorrelationExposure: 0.0,   // default 0 correlation
	}

	if h.debug {
		slog.Debug("ML scoring", "symbol", symbol, "hour", req.TimeOfDayHour, "day_of_week", req.DayOfWeek)
	}

	// Call ml-predictor service
	reqBody, _ := json.Marshal(req)
	resp, err := http.Post(
		"http://ml-predictor:8080/predict",
		"application/json",
		bytes.NewReader(reqBody),
	)
	if err != nil {
		if h.debug {
			slog.Debug("ML predictor unavailable, using default confidence", "symbol", symbol, "err", err)
		}
		// If ML service unavailable, allow signal through (don't reject on service failure)
		return 0.5, nil
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		if h.debug {
			slog.Debug("ML response read error", "symbol", symbol, "err", err)
		}
		return 0.5, nil
	}

	var mlResp MLPredictResponse
	if err := json.Unmarshal(body, &mlResp); err != nil {
		if h.debug {
			slog.Debug("ML response unmarshal error", "symbol", symbol, "err", err)
		}
		return 0.5, nil
	}

	if h.debug {
		slog.Debug("ML prediction received", "symbol", symbol, "confidence", mlResp.Confidence)
	}

	return mlResp.Confidence, nil
}

func (h *Handler) checkExposureLimits(ctx context.Context, licenseID string, accountID string, db *sql.DB) ExposureCheckResult {
	if db == nil {
		if h.debug {
			slog.Debug("no database connection for exposure limits", "license", licenseID)
		}
		return ExposureCheckResult{AllowedToProceed: true}
	}

	if h.debug {
		slog.Debug("checking exposure limits", "license", licenseID, "account", accountID)
	}

	// Query account positions
	var totalNotional float64
	query := `
		SELECT COALESCE(SUM(position_size * current_price), 0.0) as total_notional
		FROM account_positions
		WHERE license_id = $1 AND account_id = $2 AND position_size != 0
	`
	err := db.QueryRowContext(ctx, query, licenseID, accountID).Scan(&totalNotional)
	if err != nil && err != sql.ErrNoRows {
		if h.debug {
			slog.Debug("position query failed, allowing signal", "license", licenseID, "account", accountID, "err", err)
		}
		// If query fails, allow signal through
		return ExposureCheckResult{AllowedToProceed: true}
	}

	if h.debug {
		slog.Debug("current notional exposure", "license", licenseID, "account", accountID, "exposure", totalNotional)
	}

	// Query exposure limits
	query = `
		SELECT max_notional_usd
		FROM portfolio_exposure_limits
		WHERE license_id = $1 AND account_id = $2
	`
	var limitPtr *float64
	err = db.QueryRowContext(ctx, query, licenseID, accountID).Scan(&limitPtr)
	if err != nil && err != sql.ErrNoRows {
		if h.debug {
			slog.Debug("limit query failed, allowing signal", "license", licenseID, "account", accountID, "err", err)
		}
		return ExposureCheckResult{AllowedToProceed: true}
	}

	if limitPtr == nil {
		if h.debug {
			slog.Debug("no exposure limit set, allowing signal", "license", licenseID, "account", accountID)
		}
		return ExposureCheckResult{AllowedToProceed: true}
	}

	limit := *limitPtr
	if totalNotional > limit {
		if h.debug {
			slog.Debug("exposure limit exceeded", "license", licenseID, "account", accountID, "current", totalNotional, "limit", limit)
		}
		return ExposureCheckResult{
			AllowedToProceed: false,
			CurrentExposure:  totalNotional,
			ExposureLimit:    limit,
			Reason:           fmt.Sprintf("exposure_limit_exceeded: current=%.2f limit=%.2f", totalNotional, limit),
		}
	}

	if h.debug {
		slog.Debug("exposure within limits", "license", licenseID, "account", accountID, "current", totalNotional, "limit", limit)
	}

	return ExposureCheckResult{
		AllowedToProceed: true,
		CurrentExposure:  totalNotional,
		ExposureLimit:    limit,
	}
}
