package ingress

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"

	oldproto "github.com/golang/protobuf/proto"
	"github.com/ninadk/execrelay/internal/obs"
	parser "github.com/ninadk/execrelay/packages/parser-go"
)

// mlWebhookRequest is the ADR 0008 request body for POST /webhook/ml. It is
// ExecRelay-native (license_id/secret, not TradingView's x_account) so a thin
// adapter or updated Pine template supplies it.
type mlWebhookRequest struct {
	LicenseID       string         `json:"license_id"`
	Secret          string         `json:"secret"`
	Action          string         `json:"action"` // "buy" | "sell"
	Symbol          string         `json:"symbol"`
	Volume          float64        `json:"volume"`
	SL              float64        `json:"sl"`
	TP              float64        `json:"tp"`
	Comment         string         `json:"comment"`
	CurrentPosition *string        `json:"current_position"` // "LONG" | "SHORT" | null; optional
	Features        map[string]any `json:"features"`
}

// mlPredictWireRequest is the payload POSTed to ml-predictor's /predict.
type mlPredictWireRequest struct {
	Direction       int            `json:"direction"` // 1 = buy, -1 = sell
	Features        map[string]any `json:"features"`
	CurrentPosition *string        `json:"current_position,omitempty"`
}

// mlPredictResponse mirrors XGBPredictor.predict()'s return dict
// (apps/ml-predictor/xgb_predictor.py). model_version is being added by a
// parallel workstream; it's optional and simply omitted if the predictor
// doesn't send it yet.
type mlPredictResponse struct {
	SignalDirection *string  `json:"signal_direction"`
	ProbWin         *float64 `json:"prob_win"`
	Threshold       float64  `json:"threshold"`
	ShouldClose     bool     `json:"should_close"`
	ShouldOpen      bool     `json:"should_open"`
	OpenDirection   *string  `json:"open_direction"`
	ActionSummary   string   `json:"action_summary"`
	Reason          string   `json:"reason"`
	Timestamp       string   `json:"timestamp"`
	Error           *string  `json:"error"`
	ModelVersion    string   `json:"model_version,omitempty"`
}

// MLPredictor abstracts the ml-predictor /predict call so tests can inject a
// fake instead of standing up an HTTP server (though httptest servers work
// too, for the fail-open tests).
type MLPredictor interface {
	Predict(ctx context.Context, req mlPredictWireRequest) (mlPredictResponse, error)
}

// httpMLPredictor is the production MLPredictor: a plain HTTP client POSTing
// to <baseURL>/predict with a bounded timeout so a wedged predictor can never
// hang the caller past that bound (the ADR's fail-open contract depends on
// this call actually returning).
type httpMLPredictor struct {
	baseURL string
	client  *http.Client
}

func newHTTPMLPredictor(baseURL string, timeout time.Duration) *httpMLPredictor {
	return &httpMLPredictor{
		baseURL: strings.TrimRight(baseURL, "/"),
		client:  &http.Client{Timeout: timeout},
	}
}

func (p *httpMLPredictor) Predict(ctx context.Context, req mlPredictWireRequest) (mlPredictResponse, error) {
	body, err := json.Marshal(req)
	if err != nil {
		return mlPredictResponse{}, err
	}
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, p.baseURL+"/predict", bytes.NewReader(body))
	if err != nil {
		return mlPredictResponse{}, err
	}
	httpReq.Header.Set("Content-Type", "application/json")

	resp, err := p.client.Do(httpReq)
	if err != nil {
		return mlPredictResponse{}, err
	}
	defer resp.Body.Close()

	var out mlPredictResponse
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return mlPredictResponse{}, err
	}
	if resp.StatusCode >= 400 {
		msg := "predictor returned status " + strconv.Itoa(resp.StatusCode)
		if out.Error != nil && *out.Error != "" {
			msg = *out.Error
		}
		return out, errors.New(msg)
	}
	return out, nil
}

// mapDecisionToCommand implements the ADR 0008 decision->command table.
// currentPosition is the resolved position ("LONG"/"SHORT"/"") used only to
// disambiguate CLOSE_ONLY (which side is being closed). Returns
// (command, publish); publish is false for NOTHING and for a CLOSE_ONLY whose
// position can't be determined (nothing to close).
func mapDecisionToCommand(actionSummary, currentPosition string) (parser.Command, bool) {
	switch actionSummary {
	case "OPEN_LONG":
		return parser.CommandBuy, true
	case "OPEN_SHORT":
		return parser.CommandSell, true
	case "FLIP_LONG":
		return parser.CommandCloseShortOpenLong, true
	case "FLIP_SHORT":
		return parser.CommandCloseLongOpenShort, true
	case "CLOSE_ONLY":
		switch currentPosition {
		case "LONG":
			return parser.CommandCloseLong, true
		case "SHORT":
			return parser.CommandCloseShort, true
		default:
			return parser.CommandInvalid, false
		}
	case "NOTHING":
		return parser.CommandInvalid, false
	default:
		return parser.CommandInvalid, false
	}
}

// mapActionToCommand is the shadow-mode / fail-open fallback: publish the
// caller's own action untouched.
func mapActionToCommand(action string) parser.Command {
	if action == "sell" {
		return parser.CommandSell
	}
	return parser.CommandBuy
}

// buildMLSignal constructs the parser.Signal fed into the existing
// signalProto()+NATS publish path, carrying volume/sl/tp/comment as params
// exactly like the flat webhook does.
func buildMLSignal(req mlWebhookRequest, command parser.Command) parser.Signal {
	sig := parser.Signal{
		LicenseID:  req.LicenseID,
		Command:    command,
		RawCommand: command.String(),
		Symbol:     req.Symbol,
	}
	add := func(kind parser.ParamKind, key, value string) {
		if sig.ParamCount >= parser.MaxParams {
			return
		}
		sig.Params[sig.ParamCount] = parser.Param{Kind: kind, Key: key, Value: value}
		sig.ParamCount++
	}
	if req.Volume != 0 {
		add(parser.ParamVolLots, "vol_lots", strconv.FormatFloat(req.Volume, 'f', -1, 64))
	}
	if req.SL != 0 {
		add(parser.ParamSL, "sl", strconv.FormatFloat(req.SL, 'f', -1, 64))
	}
	if req.TP != 0 {
		add(parser.ParamTP, "tp", strconv.FormatFloat(req.TP, 'f', -1, 64))
	}
	if req.Comment != "" {
		add(parser.ParamComment, "comment", req.Comment)
	}
	return sig
}

// resolveCurrentPosition applies the ADR 0008 precedence: caller-supplied
// current_position always wins; otherwise fall back to the account_positions
// table; otherwise the position is unknown (treated as flat/null). Returns
// the resolved position ("LONG"/"SHORT"/"") and its source for the audit row.
func (h *Handler) resolveCurrentPosition(ctx context.Context, licenseID, accountID, symbol string, caller *string) (string, string) {
	if caller != nil {
		v := strings.ToUpper(strings.TrimSpace(*caller))
		if v == "LONG" || v == "SHORT" {
			return v, "caller"
		}
		return "", "caller" // explicit null/blank from caller: treat as flat
	}
	pos, err := h.lookupPositionFromDB(ctx, licenseID, accountID, symbol)
	if err != nil {
		return "", "unknown"
	}
	return pos, "db"
}

// lookupPositionFromDB reads the current position for (license, account,
// symbol) from account_positions (see infra/migrations/000003). Positive
// position_size = long, negative = short, zero/no-row = flat. Any failure
// (nil DB, query error, no row) is treated as "unknown" by the caller.
func (h *Handler) lookupPositionFromDB(ctx context.Context, licenseID, accountID, symbol string) (string, error) {
	if h.db == nil {
		return "", sql.ErrNoRows
	}
	ctx, cancel := context.WithTimeout(ctx, 750*time.Millisecond)
	defer cancel()
	var size float64
	err := h.db.QueryRowContext(ctx, `
		SELECT position_size FROM account_positions
		WHERE license_id = $1 AND account_id = $2 AND symbol = $3
	`, licenseID, accountID, symbol).Scan(&size)
	if err != nil {
		return "", err
	}
	switch {
	case size > 0:
		return "LONG", nil
	case size < 0:
		return "SHORT", nil
	default:
		return "", nil
	}
}

// mlDecisionRow is one row of the ml_decisions audit trail
// (infra/migrations/000006_ml_decisions.up.sql).
type mlDecisionRow struct {
	TraceID          string
	LicenseID        string
	Symbol           string
	Action           string
	ProbWin          *float64
	Threshold        float64
	ActionSummary    string
	PublishedCommand *string
	Enforced         bool
	ModelVersion     *string
	PositionSource   string
	Error            *string
}

// recordMLDecision is a best-effort, non-blocking audit insert: it must never
// slow down or fail the /webhook/ml response, and it degrades silently when
// no DB is configured (the ingress DB is optional — see readyz/checkExposureLimits
// for the same convention).
func (h *Handler) recordMLDecision(row mlDecisionRow) {
	if h.db == nil {
		return
	}
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		_, err := h.db.ExecContext(ctx, `
			INSERT INTO ml_decisions
				(trace_id, license_id, symbol, action, prob_win, threshold, action_summary,
				 published_command, enforced, model_version, position_source, error)
			VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
		`,
			row.TraceID, row.LicenseID, row.Symbol, row.Action, row.ProbWin, row.Threshold,
			row.ActionSummary, row.PublishedCommand, row.Enforced, row.ModelVersion,
			row.PositionSource, row.Error,
		)
		if err != nil {
			slog.Warn("ml decision audit: insert ml_decisions", "err", err)
		}
	}()
}

// webhookML is the ADR 0008 opt-in JSON path: it reuses the exact gating +
// per-license auth chain from webhook(), scores the request through
// ml-predictor, and publishes either the model's mapped command (enforce
// mode) or the caller's original action (shadow mode, the default). A
// predictor error/timeout always fails open: publish the original action,
// report the error, never reject a trade because the filter is down.
func (h *Handler) webhookML(w http.ResponseWriter, r *http.Request) {
	clientAddr := clientIP(r)
	reqID := obs.RequestIDFromContext(r.Context())
	if reqID == "" {
		reqID = obs.NewID()
	}
	wctx := &webhookCtx{requestID: reqID}
	rec := &webhookRecorder{ResponseWriter: w, status: http.StatusOK}
	start := h.now()
	defer h.recordRequestEvent(r, rec, wctx, clientAddr, start)
	w = rec

	if h.debug {
		slog.Debug("webhook/ml request received", "request_id", reqID, "client", clientAddr, "method", r.Method)
	}

	body, ok := h.gatingPreamble(w, r, wctx, clientAddr)
	if !ok {
		return
	}

	var req mlWebhookRequest
	if err := json.Unmarshal(body, &req); err != nil {
		if h.debug {
			slog.Debug("ml parse error", "client", clientAddr, "err", err)
		}
		wctx.reasonCode = "parse_error"
		recordMLOutcome("rejected")
		writeJSONAny(w, http.StatusBadRequest, map[string]string{"error": "parse_error", "reason": err.Error()})
		return
	}
	wctx.licenseKey = req.LicenseID

	action := strings.ToLower(strings.TrimSpace(req.Action))
	if action != "buy" && action != "sell" {
		wctx.reasonCode = "invalid_action"
		recordMLOutcome("rejected")
		writeJSONAny(w, http.StatusBadRequest, map[string]string{"error": "invalid_action", "reason": "action must be \"buy\" or \"sell\""})
		return
	}
	if req.LicenseID == "" || req.Symbol == "" {
		wctx.reasonCode = "missing_field"
		recordMLOutcome("rejected")
		writeJSONAny(w, http.StatusBadRequest, map[string]string{"error": "missing_field", "reason": "license_id and symbol are required"})
		return
	}

	record, ok := h.authenticate(r, w, wctx, clientAddr, body, req.LicenseID, req.Secret)
	if !ok {
		recordMLOutcome("rejected")
		return
	}

	traceID := traceIDFromRequest(r.Header)
	if traceID == "" {
		if ctxTrace := obs.TraceIDFromContext(r.Context()); ctxTrace != "" {
			traceID = ctxTrace
		} else {
			traceID = newTraceID()
		}
	}
	wctx.traceID = traceID

	var direction int
	if action == "sell" {
		direction = -1
	} else {
		direction = 1
	}

	currentPosition, positionSource := h.resolveCurrentPosition(r.Context(), req.LicenseID, record.InstanceID, req.Symbol, req.CurrentPosition)

	var resolvedPosPtr *string
	if currentPosition != "" {
		resolvedPosPtr = &currentPosition
	}
	predictCtx, cancel := context.WithTimeout(r.Context(), h.mlPredictTimeout)
	resp, predictErr := h.mlPredictor.Predict(predictCtx, mlPredictWireRequest{
		Direction:       direction,
		Features:        req.Features,
		CurrentPosition: resolvedPosPtr,
	})
	cancel()

	enforced := h.mlEnforce
	var command parser.Command
	var publish bool
	var status string
	var mlOut map[string]any
	auditRow := mlDecisionRow{
		TraceID:        traceID,
		LicenseID:      req.LicenseID,
		Symbol:         req.Symbol,
		Action:         action,
		Enforced:       enforced,
		PositionSource: positionSource,
	}

	respErr := predictErr
	if respErr == nil && resp.Error != nil && *resp.Error != "" {
		respErr = errors.New(*resp.Error)
	}

	if respErr != nil {
		// Fail open: never reject a trade because the ML filter is down.
		mlPredictorErrors.Inc()
		command = mapActionToCommand(action)
		publish = true
		status = "accepted"
		errMsg := respErr.Error()
		mlOut = map[string]any{"error": errMsg, "enforced": enforced}
		auditRow.Error = &errMsg
		auditRow.ActionSummary = "NOTHING"
		recordMLOutcome("fail_open")
	} else {
		mappedCmd, shouldPublish := mapDecisionToCommand(resp.ActionSummary, currentPosition)
		if enforced {
			command = mappedCmd
			publish = shouldPublish
		} else {
			// Shadow mode: always publish the caller's original action; the
			// response still reports what the model would have done.
			command = mapActionToCommand(action)
			publish = true
		}
		if publish {
			status = "accepted"
		} else {
			status = "skipped"
		}

		var modelVersion *string
		if resp.ModelVersion != "" {
			mv := resp.ModelVersion
			modelVersion = &mv
		}
		mlOut = map[string]any{
			"action_summary": resp.ActionSummary,
			"prob_win":       resp.ProbWin,
			"threshold":      resp.Threshold,
			"model_version":  resp.ModelVersion,
			"enforced":       enforced,
		}
		auditRow.ProbWin = resp.ProbWin
		auditRow.Threshold = resp.Threshold
		auditRow.ActionSummary = resp.ActionSummary
		auditRow.ModelVersion = modelVersion
		recordMLOutcome(status)
	}

	if publish {
		cmdStr := command.String()
		auditRow.PublishedCommand = &cmdStr
		sig := buildMLSignal(req, command)
		wire := signalProto(sig, record, h.region, traceID, body, h.now())
		payload, err := oldproto.Marshal(wire)
		if err != nil {
			if h.debug {
				slog.Debug("ml protobuf encoding failed", "trace_id", traceID, "err", err)
			}
			wctx.reasonCode = "encode_failed"
			recordMLOutcome("rejected")
			writeJSONAny(w, http.StatusInternalServerError, map[string]string{"error": "encode_failed"})
			h.recordMLDecision(auditRow)
			return
		}
		subject := signalSubject(req.LicenseID, record.InstanceID, record.Platform)
		if err := h.publisher.Publish(r.Context(), subject, payload); err != nil {
			if h.debug {
				slog.Debug("ml publish failed", "trace_id", traceID, "subject", subject, "err", err)
			}
			wctx.reasonCode = "publish_failed"
			recordMLOutcome("rejected")
			writeJSONAny(w, http.StatusServiceUnavailable, map[string]string{"error": "publish_failed"})
			h.recordMLDecision(auditRow)
			return
		}
	}

	h.recordMLDecision(auditRow)

	wctx.reasonCode = status
	w.Header().Set("X-ExecRelay-Trace-ID", traceID)
	writeJSONAny(w, http.StatusOK, map[string]any{
		"status":   status,
		"trace_id": traceID,
		"ml":       mlOut,
	})
}
