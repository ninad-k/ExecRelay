package ingress

import (
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"math"
	"net"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	oldproto "github.com/golang/protobuf/proto"
	"github.com/ninadk/execrelay/internal/obs"
	parser "github.com/ninadk/execrelay/packages/parser-go"
	execrelaypb "github.com/ninadk/execrelay/packages/proto"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

type Handler struct {
	store            LicenseStore
	publisher        Publisher
	eventPublisher   Publisher
	region           string
	maxBodyBytes     int64
	now              func() time.Time
	timestampWindow  time.Duration
	rateLimiter      *ipRateLimiter
	allowedCIDRs     []*net.IPNet
	dailyCounter     *dailyCounter
	db               *sql.DB
	perimeterToken   []byte // empty = gate disabled
	tradingHalted    atomic.Bool
	debug            bool
	mlPredictor      MLPredictor
	mlEnforce        bool // false (default) = shadow mode: score + audit, but always publish the caller's action
	mlPredictTimeout time.Duration
}

type Options struct {
	Store           LicenseStore
	Publisher       Publisher
	EventPublisher  Publisher
	Region          string
	MaxBodyBytes    int64
	Now             func() time.Time
	TimestampWindow time.Duration
	RateLimit       int // max requests per minute per IP; 0 = disabled
	AllowedCIDRs    []*net.IPNet
	DB              *sql.DB
	PerimeterToken  string // optional shared secret required as ?token=<value>; empty = disabled
	TradingHalted   bool   // initial state of the kill switch; can be toggled later via /admin/halt
	Debug           bool

	// MLPredictor overrides the default HTTP client used to call ml-predictor.
	// Tests inject a fake here; production leaves it nil and NewHandler builds
	// one from MLPredictorURL/MLPredictTimeout.
	MLPredictor      MLPredictor
	MLPredictorURL   string        // base URL of ml-predictor; default http://ml-predictor:8080
	MLPredictTimeout time.Duration // /predict call timeout; default 2s
	MLEnforce        bool          // false (default) = shadow mode; see ADR 0008
}

func NewHandler(opts Options) *Handler {
	if opts.Publisher == nil {
		opts.Publisher = NoopPublisher{}
	}
	if opts.Store == nil {
		opts.Store = NewStaticLicenseStore(nil)
	}
	if opts.Region == "" {
		opts.Region = defaultRegion
	}
	if opts.MaxBodyBytes <= 0 {
		opts.MaxBodyBytes = defaultMaxBodyBytes
	}
	if opts.Now == nil {
		opts.Now = time.Now
	}
	var rl *ipRateLimiter
	if opts.RateLimit > 0 {
		rl = newIPRateLimiter(float64(opts.RateLimit)/60.0, opts.RateLimit)
	}
	var perimeter []byte
	if opts.PerimeterToken != "" {
		perimeter = []byte(opts.PerimeterToken)
	}
	if opts.MLPredictTimeout <= 0 {
		opts.MLPredictTimeout = defaultMLPredictTimeout
	}
	predictor := opts.MLPredictor
	if predictor == nil {
		url := opts.MLPredictorURL
		if url == "" {
			url = defaultMLPredictorURL
		}
		predictor = newHTTPMLPredictor(url, opts.MLPredictTimeout)
	}
	h := &Handler{
		store:            opts.Store,
		publisher:        opts.Publisher,
		eventPublisher:   opts.EventPublisher,
		region:           opts.Region,
		maxBodyBytes:     opts.MaxBodyBytes,
		now:              opts.Now,
		timestampWindow:  opts.TimestampWindow,
		rateLimiter:      rl,
		allowedCIDRs:     opts.AllowedCIDRs,
		dailyCounter:     newDailyCounter(),
		db:               opts.DB,
		perimeterToken:   perimeter,
		debug:            opts.Debug,
		mlPredictor:      predictor,
		mlEnforce:        opts.MLEnforce,
		mlPredictTimeout: opts.MLPredictTimeout,
	}
	h.tradingHalted.Store(opts.TradingHalted)
	reportTradingHalted(opts.TradingHalted)
	return h
}

func (h *Handler) Routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/health", h.health)
	mux.HandleFunc("/healthz", h.health)
	mux.HandleFunc("/readyz", h.readyz)
	mux.HandleFunc("/webhook", h.webhook)
	mux.HandleFunc("/webhook/ml", h.webhookML)
	mux.HandleFunc("/admin/kill-switch", h.killSwitch)
	mux.Handle("/metrics", promhttp.Handler())
	return obs.Middleware("ingress")(metricsMiddleware(mux))
}

func (h *Handler) health(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"service": "ingress", "status": "ok"})
}

// readyz reports whether the ingress can do its job: NATS publisher healthy
// and (if a DB is configured for exposure checks) DB reachable. Returns 503
// with per-check detail so a load balancer can pull this instance.
func (h *Handler) readyz(w http.ResponseWriter, r *http.Request) {
	checks := map[string]any{}
	ok := true

	if hp, isHealth := h.publisher.(interface{ Healthy() bool }); isHealth {
		alive := hp.Healthy()
		checks["nats"] = map[string]any{"ok": alive}
		if !alive {
			ok = false
		}
	} else {
		checks["nats"] = map[string]any{"ok": true, "note": "publisher does not report health"}
	}

	if h.db != nil {
		ctx, cancel := context.WithTimeout(r.Context(), 750*time.Millisecond)
		defer cancel()
		if err := h.db.PingContext(ctx); err != nil {
			checks["db"] = map[string]any{"ok": false, "err": err.Error()}
			ok = false
		} else {
			checks["db"] = map[string]any{"ok": true}
		}
	}

	body, _ := json.Marshal(map[string]any{
		"service": "ingress",
		"ok":      ok,
		"checks":  checks,
	})
	w.Header().Set("Content-Type", "application/json")
	if ok {
		w.WriteHeader(http.StatusOK)
	} else {
		w.WriteHeader(http.StatusServiceUnavailable)
	}
	_, _ = w.Write(body)
}

// killSwitch reports or toggles the trading-halt flag.
//
//	GET  /admin/kill-switch?token=<perimeter>           — returns {"halted": "true|false"}
//	POST /admin/kill-switch?token=<perimeter>&state=on  — halts trading
//	POST /admin/kill-switch?token=<perimeter>&state=off — resumes trading
//
// Always requires the perimeter token (separate from per-license auth) so a
// misconfigured license cannot accidentally lift a halt. Toggle changes are
// logged with the client IP for audit; the token value itself is never logged.
func (h *Handler) killSwitch(w http.ResponseWriter, r *http.Request) {
	clientAddr := clientIP(r)
	if len(h.perimeterToken) == 0 {
		// Without a perimeter token the kill switch is unreachable to prevent
		// a wide-open endpoint from being toggled by anyone on the network.
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "kill_switch_disabled", "reason": "INGRESS_PERIMETER_TOKEN must be set to use this endpoint"})
		return
	}
	got := r.URL.Query().Get("token")
	if !hmac.Equal([]byte(got), h.perimeterToken) {
		recordRejection("perimeter_rejected")
		writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "perimeter_rejected"})
		return
	}

	switch r.Method {
	case http.MethodGet:
		writeJSON(w, http.StatusOK, map[string]string{
			"halted": strconv.FormatBool(h.tradingHalted.Load()),
		})
	case http.MethodPost:
		state := strings.ToLower(strings.TrimSpace(r.URL.Query().Get("state")))
		var halted bool
		switch state {
		case "on", "halt", "halted", "true", "1":
			halted = true
		case "off", "resume", "false", "0":
			halted = false
		default:
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid_state", "reason": "state must be on|off"})
			return
		}
		previous := h.tradingHalted.Swap(halted)
		reportTradingHalted(halted)
		if previous != halted {
			slog.Warn("kill switch toggled", "client", clientAddr, "halted", halted, "previous", previous)
			h.recordKillSwitchEvent(r.Context(), clientAddr, halted, previous)
		}
		writeJSON(w, http.StatusOK, map[string]string{
			"halted":   strconv.FormatBool(halted),
			"previous": strconv.FormatBool(previous),
		})
	default:
		w.Header().Set("Allow", "GET, POST")
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method_not_allowed"})
	}
}

// recordKillSwitchEvent persists a structured audit record of a kill-switch
// state change to the system_events table. Best-effort: a logging failure must
// never block the toggle itself, so errors are logged and swallowed. The
// perimeter token is deliberately never included in the payload.
func (h *Handler) recordKillSwitchEvent(ctx context.Context, clientAddr string, halted, previous bool) {
	if h.db == nil {
		return
	}
	severity := "warning"
	if halted {
		severity = "critical"
	}
	payload, err := json.Marshal(map[string]any{
		"client":   clientAddr,
		"halted":   halted,
		"previous": previous,
	})
	if err != nil {
		slog.Error("kill switch audit: marshal payload", "err", err)
		return
	}
	ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	if _, err := h.db.ExecContext(ctx,
		`INSERT INTO system_events (event_type, severity, payload) VALUES ($1, $2, $3::jsonb)`,
		"kill_switch_toggled", severity, string(payload),
	); err != nil {
		slog.Error("kill switch audit: insert system_events", "err", err)
	}
}

// webhookCtx threads outcome metadata from the body of the handler back to
// the deferred publishRequestEvent. The handler updates these fields as it
// learns more (license_key, trace_id, body_hash, reason_code).
type webhookCtx struct {
	requestID  string
	traceID    string
	licenseKey string
	bodySHA256 string
	reasonCode string
}

func (h *Handler) webhook(w http.ResponseWriter, r *http.Request) {
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
		slog.Debug("webhook request received", "request_id", reqID, "client", clientAddr, "method", r.Method)
	}

	body, ok := h.gatingPreamble(w, r, wctx, clientAddr)
	if !ok {
		return
	}
	raw := string(body)

	parsed, err := parser.Parse(raw)
	if err != nil {
		if h.debug {
			slog.Debug("parse error", "client", clientAddr, "err", err)
		}
		wctx.reasonCode = "parse_error"
		writeJSON(w, http.StatusBadRequest, reject("parse_error", err))
		return
	}
	wctx.licenseKey = parsed.LicenseID
	if h.debug {
		slog.Debug("signal parsed", "client", clientAddr, "license", parsed.LicenseID, "symbol", parsed.Symbol, "command", parsed.RawCommand)
	}

	secretValue := ""
	if param, ok := parsed.Param(parser.ParamSecret); ok {
		secretValue = param.Value
	}
	record, ok := h.authenticate(r, w, wctx, clientAddr, body, parsed.LicenseID, secretValue)
	if !ok {
		return
	}

	traceID := traceIDFromRequest(r.Header)
	if traceID == "" {
		// Prefer the middleware-assigned trace_id if the caller didn't bring
		// one — keeps log lines and the published Signal in lockstep.
		if ctxTrace := obs.TraceIDFromContext(r.Context()); ctxTrace != "" {
			traceID = ctxTrace
		} else {
			traceID = newTraceID()
		}
	}
	wctx.traceID = traceID
	if h.debug {
		slog.Debug("trace ID assigned", "trace_id", traceID, "license", parsed.LicenseID)
	}

	wire := signalProto(parsed, record, h.region, traceID, body, h.now())
	payload, err := oldproto.Marshal(wire)
	if err != nil {
		if h.debug {
			slog.Debug("protobuf encoding failed", "trace_id", traceID, "err", err)
		}
		wctx.reasonCode = "encode_failed"
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "encode_failed"})
		return
	}
	if h.debug {
		slog.Debug("signal encoded", "trace_id", traceID, "payload_size", len(payload))
	}

	subject := signalSubject(parsed.LicenseID, record.InstanceID, record.Platform)
	if h.debug {
		slog.Debug("publishing signal", "trace_id", traceID, "subject", subject)
	}
	if err := h.publisher.Publish(r.Context(), subject, payload); err != nil {
		if h.debug {
			slog.Debug("publish failed", "trace_id", traceID, "subject", subject, "err", err)
		}
		wctx.reasonCode = "publish_failed"
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "publish_failed"})
		return
	}
	if h.debug {
		slog.Debug("signal published successfully", "trace_id", traceID, "license", parsed.LicenseID, "symbol", parsed.Symbol)
	}

	wctx.reasonCode = "accepted"
	w.Header().Set("X-ExecRelay-Trace-ID", traceID)
	writeJSON(w, http.StatusOK, map[string]string{"status": "accepted", "trace_id": traceID})
}

// gatingPreamble runs the perimeter checks shared by every ingress webhook
// route, in the order the ADR 0008 spec requires: method, perimeter token,
// kill-switch, per-IP rate limit, CIDR allowlist, timestamp window, then the
// raw body read (bounded by maxBodyBytes). It writes the appropriate error
// response and sets wctx.reasonCode itself on rejection. Callers (webhook,
// webhookML) parse the returned body in their own wire format afterwards.
func (h *Handler) gatingPreamble(w http.ResponseWriter, r *http.Request, wctx *webhookCtx, clientAddr string) ([]byte, bool) {
	if r.Method != http.MethodPost {
		if h.debug {
			slog.Debug("rejecting non-POST request", "client", clientAddr, "method", r.Method)
		}
		w.Header().Set("Allow", http.MethodPost)
		wctx.reasonCode = "method_not_allowed"
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method_not_allowed"})
		return nil, false
	}

	if len(h.perimeterToken) > 0 {
		// Constant-time check; never log the supplied token value.
		got := r.URL.Query().Get("token")
		if !hmac.Equal([]byte(got), h.perimeterToken) {
			if h.debug {
				slog.Debug("perimeter token rejected", "client", clientAddr)
			}
			recordRejection("perimeter_rejected")
			wctx.reasonCode = "perimeter_rejected"
			writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "perimeter_rejected"})
			return nil, false
		}
	}

	if h.tradingHalted.Load() {
		recordRejection("trading_halted")
		wctx.reasonCode = "trading_halted"
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "trading_halted"})
		return nil, false
	}

	if h.rateLimiter != nil && !h.rateLimiter.allow(clientAddr) {
		if h.debug {
			slog.Debug("rate limit exceeded", "client", clientAddr)
		}
		recordRejection("rate_limit_exceeded")
		wctx.reasonCode = "rate_limit_exceeded"
		writeJSON(w, http.StatusTooManyRequests, map[string]string{"error": "rate_limit_exceeded"})
		return nil, false
	}

	if len(h.allowedCIDRs) > 0 {
		ip := net.ParseIP(clientAddr)
		allowed := false
		if ip != nil {
			for _, cidr := range h.allowedCIDRs {
				if cidr.Contains(ip) {
					allowed = true
					break
				}
			}
		}
		if !allowed {
			if h.debug {
				slog.Debug("IP not in allowed CIDRs", "client", clientAddr)
			}
			recordRejection("ip_not_allowed")
			wctx.reasonCode = "ip_not_allowed"
			writeJSON(w, http.StatusForbidden, map[string]string{"error": "ip_not_allowed"})
			return nil, false
		}
		if h.debug {
			slog.Debug("IP allowed by CIDR", "client", clientAddr)
		}
	}

	if h.timestampWindow > 0 {
		if err := checkTimestamp(r.Header, h.now(), h.timestampWindow); err != nil {
			if h.debug {
				slog.Debug("timestamp validation failed", "client", clientAddr, "err", err)
			}
			recordRejection("timestamp_rejected")
			wctx.reasonCode = "timestamp_rejected"
			writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "timestamp_rejected", "reason": err.Error()})
			return nil, false
		}
		if h.debug {
			slog.Debug("timestamp valid", "client", clientAddr)
		}
	}

	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, h.maxBodyBytes))
	if err != nil {
		if h.debug {
			slog.Debug("failed to read body", "client", clientAddr, "err", err)
		}
		wctx.reasonCode = "body_too_large"
		writeJSON(w, http.StatusRequestEntityTooLarge, map[string]string{"error": "body_too_large"})
		return nil, false
	}
	if len(body) > 0 {
		hash := sha256.Sum256(body)
		wctx.bodySHA256 = hex.EncodeToString(hash[:])
	}
	if h.debug {
		slog.Debug("body received", "client", clientAddr, "size", len(body))
	}

	return body, true
}

// authenticate runs the per-license gating chain shared by every ingress
// webhook route: license lookup, subject-token validation, secret check,
// HMAC-over-raw-body verification, daily quota, and exposure-limit checks.
// It writes the appropriate error response and sets wctx.reasonCode itself
// on rejection, mirroring gatingPreamble's contract.
func (h *Handler) authenticate(r *http.Request, w http.ResponseWriter, wctx *webhookCtx, clientAddr string, body []byte, licenseID, secretValue string) (LicenseRecord, bool) {
	record, err := h.store.Lookup(r.Context(), licenseID)
	if err != nil {
		if h.debug {
			slog.Debug("license lookup failed", "client", clientAddr, "license", licenseID, "err", err)
		}
		status := http.StatusUnauthorized
		if errors.Is(err, ErrLicenseInactive) {
			status = http.StatusForbidden
		}
		h.publishRejection(licenseID, "license_rejected", body)
		wctx.reasonCode = "license_rejected"
		writeJSON(w, status, map[string]string{"error": "license_rejected"})
		return LicenseRecord{}, false
	}
	if h.debug {
		slog.Debug("license found", "client", clientAddr, "license", licenseID, "instance", record.InstanceID)
	}

	if !validSubjectToken(licenseID) || !validSubjectToken(record.InstanceID) {
		if h.debug {
			slog.Debug("invalid subject tokens", "client", clientAddr, "license", licenseID)
		}
		wctx.reasonCode = "invalid_route_token"
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid_route_token"})
		return LicenseRecord{}, false
	}
	if record.Secret != "" && !constantStringEqual(secretValue, record.Secret) {
		if h.debug {
			slog.Debug("secret validation failed", "client", clientAddr, "license", licenseID)
		}
		h.publishRejection(licenseID, "secret_rejected", body)
		wctx.reasonCode = "secret_rejected"
		writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "secret_rejected"})
		return LicenseRecord{}, false
	}
	if h.debug && record.Secret != "" {
		slog.Debug("secret validated", "client", clientAddr, "license", licenseID)
	}

	if record.HMACSecret != "" {
		primaryOK := validSignature(body, record.HMACSecret, r.Header)
		pendingOK := record.PendingHMACSecret != "" && validSignature(body, record.PendingHMACSecret, r.Header)
		if !primaryOK && !pendingOK {
			if h.debug {
				slog.Debug("signature validation failed", "client", clientAddr, "license", licenseID)
			}
			h.publishRejection(licenseID, "signature_rejected", body)
			wctx.reasonCode = "signature_rejected"
			writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "signature_rejected"})
			return LicenseRecord{}, false
		}
		if h.debug {
			slog.Debug("signature validated", "client", clientAddr, "license", licenseID, "primary", primaryOK)
		}
	}

	if record.MaxSignalsPerDay > 0 {
		count := h.dailyCounter.Increment(licenseID, h.now())
		if h.debug {
			slog.Debug("daily signal count", "license", licenseID, "count", count, "limit", record.MaxSignalsPerDay)
		}
		if count > record.MaxSignalsPerDay {
			if h.debug {
				slog.Debug("daily plan limit exceeded", "client", clientAddr, "license", licenseID)
			}
			recordRejection("plan_limit_exceeded")
			wctx.reasonCode = "plan_limit_exceeded"
			writeJSON(w, http.StatusTooManyRequests, map[string]string{"error": "plan_limit_exceeded"})
			return LicenseRecord{}, false
		}
	}

	// Check exposure limits (Phase 7)
	if h.db != nil && record.InstanceID != "" {
		exposure := h.checkExposureLimits(r.Context(), licenseID, record.InstanceID, h.db)
		if h.debug {
			slog.Debug("exposure check", "license", licenseID, "account", record.InstanceID, "current", exposure.CurrentExposure, "limit", exposure.ExposureLimit)
		}
		if !exposure.AllowedToProceed {
			if h.debug {
				slog.Debug("exposure limit exceeded", "client", clientAddr, "license", licenseID, "reason", exposure.Reason)
			}
			recordRejection("exposure_limit_exceeded")
			h.publishRejection(licenseID, "exposure_limit_exceeded", body)
			wctx.reasonCode = "exposure_limit_exceeded"
			writeJSON(w, http.StatusTooManyRequests, map[string]string{"error": "exposure_limit_exceeded", "reason": exposure.Reason})
			return LicenseRecord{}, false
		}
	}

	return record, true
}

// webhookRecorder lets the deferred request-log publisher know the final
// status code without each return path having to wire it explicitly.
type webhookRecorder struct {
	http.ResponseWriter
	status int
}

func (r *webhookRecorder) WriteHeader(code int) {
	r.status = code
	r.ResponseWriter.WriteHeader(code)
}

// recordRequestEvent publishes one events.ingress.request message per
// webhook attempt (accept or reject). persist consumes the subject and
// writes the row to request_log so `GET /requests/{request_id}` returns
// full context for any past call.
func (h *Handler) recordRequestEvent(r *http.Request, rec *webhookRecorder, wctx *webhookCtx, clientAddr string, start time.Time) {
	if h.eventPublisher == nil {
		return
	}
	outcome := "error"
	switch {
	case rec.status >= 200 && rec.status < 300:
		outcome = "accepted"
	case rec.status >= 400 && rec.status < 500:
		outcome = "rejected"
	}
	if wctx.reasonCode == "" {
		wctx.reasonCode = outcome
	}
	evt := map[string]any{
		"service":     "ingress",
		"request_id":  wctx.requestID,
		"trace_id":    wctx.traceID,
		"license_key": wctx.licenseKey,
		"method":      r.Method,
		"path":        r.URL.Path,
		"client_ip":   clientAddr,
		"status":      rec.status,
		"outcome":     outcome,
		"reason_code": wctx.reasonCode,
		"latency_ms":  int(time.Since(start).Milliseconds()),
		"body_sha256": wctx.bodySHA256,
		"user_agent":  truncateStr(r.UserAgent(), 240),
		"region":      h.region,
		"received_at": start.UTC().Format(time.RFC3339Nano),
	}
	data, err := json.Marshal(evt)
	if err != nil {
		slog.Warn("marshal request event", "err", err)
		return
	}
	if err := h.eventPublisher.Publish(context.Background(), "events.ingress.request", data); err != nil {
		slog.Warn("publish request event", "err", err)
	}
}

func truncateStr(s string, max int) string {
	if len(s) <= max {
		return s
	}
	return s[:max]
}

func (h *Handler) publishRejection(licenseID, reason string, body []byte) {
	if h.eventPublisher == nil {
		return
	}
	hash := sha256.Sum256(body)
	data, _ := json.Marshal(map[string]string{
		"license_id":   licenseID,
		"reason_code":  reason,
		"payload_hash": hex.EncodeToString(hash[:]),
		"region":       h.region,
	})
	if err := h.eventPublisher.Publish(context.Background(), "events.ingress.rejection", data); err != nil {
		slog.Warn("publish rejection event", "err", err)
	}
}

func checkTimestamp(header http.Header, now time.Time, window time.Duration) error {
	raw := header.Get("X-ExecRelay-Timestamp")
	if raw == "" {
		return nil // header absent → skip (backward-compatible)
	}
	ts, err := strconv.ParseInt(strings.TrimSpace(raw), 10, 64)
	if err != nil {
		return errors.New("invalid timestamp format")
	}
	diff := now.Sub(time.Unix(ts, 0))
	if diff < 0 {
		diff = -diff
	}
	if diff > window {
		return errors.New("timestamp outside acceptable window")
	}
	return nil
}

func signalProto(signal parser.Signal, record LicenseRecord, region, traceID string, body []byte, received time.Time) *execrelaypb.Signal {
	hash := sha256.Sum256(body)
	wire := &execrelaypb.Signal{
		TraceId:          traceID,
		LicenseId:        signal.LicenseID,
		InstanceId:       record.InstanceID,
		Command:          signal.Command.String(),
		RawCommand:       signal.RawCommand,
		Symbol:           signal.Symbol,
		IngressRegion:    region,
		ReceivedUnixNano: received.UnixNano(),
		BodySha256:       hex.EncodeToString(hash[:]),
		Params:           make([]*execrelaypb.SignalParam, 0, signal.ParamCount),
	}
	for i := 0; i < signal.ParamCount; i++ {
		param := signal.Params[i]
		if param.Kind == parser.ParamSecret {
			continue
		}
		wire.Params = append(wire.Params, &execrelaypb.SignalParam{Key: param.Key, Value: param.Value})
	}
	return wire
}

func validSignature(body []byte, secret string, header http.Header) bool {
	signature := header.Get("X-ExecRelay-Signature")
	if signature == "" {
		signature = header.Get("X-Signature")
	}
	if signature == "" {
		signature = header.Get("X-Hub-Signature-256")
	}
	if strings.HasPrefix(signature, "sha256=") {
		signature = signature[len("sha256="):]
	}
	if len(signature) != sha256.Size*2 {
		return false
	}

	got, err := hex.DecodeString(signature)
	if err != nil {
		return false
	}
	mac := hmac.New(sha256.New, []byte(secret))
	_, _ = mac.Write(body)
	want := mac.Sum(nil)
	return hmac.Equal(got, want)
}

func signalSubject(licenseID, instanceID, platform string) string {
	if platform == "" {
		platform = "mt5"
	}
	return "signals." + platform + "." + licenseID + "." + instanceID
}

func validSubjectToken(value string) bool {
	if value == "" {
		return false
	}
	for i := 0; i < len(value); i++ {
		c := value[i]
		if (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_' || c == '-' {
			continue
		}
		return false
	}
	return true
}

func traceIDFromRequest(header http.Header) string {
	if traceID := strings.TrimSpace(header.Get("X-ExecRelay-Trace-ID")); traceID != "" {
		return traceID
	}
	traceparent := header.Get("Traceparent")
	if len(traceparent) >= 55 && traceparent[2] == '-' && traceparent[35] == '-' {
		return traceparent[3:35]
	}
	return ""
}

func newTraceID() string {
	var bytes [16]byte
	if _, err := rand.Read(bytes[:]); err != nil {
		return strconv.FormatInt(time.Now().UnixNano(), 16)
	}
	return hex.EncodeToString(bytes[:])
}

func clientIP(r *http.Request) string {
	if xff := r.Header.Get("X-Forwarded-For"); xff != "" {
		if i := strings.IndexByte(xff, ','); i > 0 {
			return strings.TrimSpace(xff[:i])
		}
		return strings.TrimSpace(xff)
	}
	if ip := r.Header.Get("X-Real-IP"); ip != "" {
		return strings.TrimSpace(ip)
	}
	addr := r.RemoteAddr
	if i := strings.LastIndexByte(addr, ':'); i > 0 {
		return addr[:i]
	}
	return addr
}

func reject(code string, err error) map[string]string {
	response := map[string]string{"error": code}
	if parseErr, ok := err.(parser.ParseError); ok {
		response["reason"] = parseErr.Error()
		response["field"] = parseErr.Field
		return response
	}
	response["reason"] = err.Error()
	return response
}

func writeJSON(w http.ResponseWriter, status int, payload map[string]string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}

// writeJSONAny is writeJSON's counterpart for handlers (like webhookML) whose
// response body has nested/typed fields (e.g. "ml": {"prob_win": 0.63, ...})
// that don't fit the flat map[string]string shape.
func writeJSONAny(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}

func Shutdown(ctx context.Context, publisher Publisher) error {
	done := make(chan struct{})
	go func() {
		publisher.Close()
		close(done)
	}()

	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-done:
		return nil
	}
}

// ipRateLimiter is a simple per-IP token bucket (no external deps).
type ipRateLimiter struct {
	mu      sync.Mutex
	buckets map[string]*tokenBucket
	rate    float64 // tokens per second
	burst   int
}

type tokenBucket struct {
	tokens float64
	last   time.Time
}

const bucketIdleExpiry = 10 * time.Minute

func newIPRateLimiter(ratePerSec float64, burst int) *ipRateLimiter {
	l := &ipRateLimiter{
		buckets: make(map[string]*tokenBucket),
		rate:    ratePerSec,
		burst:   burst,
	}
	go l.cleanupLoop()
	return l
}

func (l *ipRateLimiter) allow(ip string) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	b, ok := l.buckets[ip]
	if !ok {
		b = &tokenBucket{tokens: float64(l.burst), last: time.Now()}
		l.buckets[ip] = b
	}
	now := time.Now()
	elapsed := now.Sub(b.last).Seconds()
	b.tokens = math.Min(float64(l.burst), b.tokens+elapsed*l.rate)
	b.last = now
	if b.tokens >= 1 {
		b.tokens--
		return true
	}
	return false
}

// cleanupLoop removes buckets that haven't been seen for bucketIdleExpiry.
func (l *ipRateLimiter) cleanupLoop() {
	ticker := time.NewTicker(bucketIdleExpiry / 2)
	defer ticker.Stop()
	for range ticker.C {
		cutoff := time.Now().Add(-bucketIdleExpiry)
		l.mu.Lock()
		for ip, b := range l.buckets {
			if b.last.Before(cutoff) {
				delete(l.buckets, ip)
			}
		}
		l.mu.Unlock()
	}
}
