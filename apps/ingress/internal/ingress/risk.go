package ingress

import (
	"context"
	"database/sql"
	"fmt"
	"log/slog"
)

type ExposureCheckResult struct {
	AllowedToProceed bool
	CurrentExposure  float64
	ExposureLimit    float64
	Reason           string
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
