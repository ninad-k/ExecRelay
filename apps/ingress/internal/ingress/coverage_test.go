package ingress

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"hash"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func hmacNew(secret string) hash.Hash { return hmac.New(sha256.New, []byte(secret)) }
func hexEncode(b []byte) string       { return hex.EncodeToString(b) }

// These tests round out coverage on the cheap, deterministic pieces of the
// package: config parsing, license stores, rate limiter, counter, audit.
// The wire-format hot path is already covered by handler_test.go.

// ---- config / license parsing ----------------------------------------------

func TestParseLicenseRecords_EmptyReturnsNil(t *testing.T) {
	got, err := ParseLicenseRecords("")
	if err != nil || got != nil {
		t.Fatalf("got %v, %v", got, err)
	}
}

func TestParseLicenseRecords_HappyPath(t *testing.T) {
	raw := "L1:secret1:hmac1:I1:mt5;L2:secret2:hmac2:I2:mt4:pending2:100"
	recs, err := ParseLicenseRecords(raw)
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if len(recs) != 2 {
		t.Fatalf("expected 2 records, got %d", len(recs))
	}
	if recs[0].LicenseID != "L1" || recs[0].Platform != "mt5" {
		t.Fatalf("record 0 wrong: %+v", recs[0])
	}
	if recs[1].PendingHMACSecret != "pending2" || recs[1].MaxSignalsPerDay != 100 {
		t.Fatalf("record 1 wrong: %+v", recs[1])
	}
}

func TestParseLicenseRecords_DefaultsPlatformWhenMissing(t *testing.T) {
	recs, err := ParseLicenseRecords("L1:secret:hmac:I1")
	if err != nil || len(recs) != 1 {
		t.Fatalf("err=%v recs=%+v", err, recs)
	}
	if recs[0].Platform != "mt5" {
		t.Fatalf("expected default platform mt5, got %q", recs[0].Platform)
	}
}

func TestParseLicenseRecords_RejectsBadFieldCount(t *testing.T) {
	if _, err := ParseLicenseRecords("only:two:fields"); err == nil {
		t.Fatal("expected error on too-few fields")
	}
}

func TestParseLicenseRecords_RejectsBadMaxSignals(t *testing.T) {
	_, err := ParseLicenseRecords("L1:secret:hmac:I1:mt5:pending:not-a-number")
	if err == nil {
		t.Fatal("expected error on non-integer max")
	}
}

func TestParseLicenseRecords_RejectsEmptyLicenseOrInstance(t *testing.T) {
	if _, err := ParseLicenseRecords(":secret:hmac:I1"); err == nil {
		t.Fatal("expected error on empty license_id")
	}
	if _, err := ParseLicenseRecords("L1:secret:hmac:"); err == nil {
		t.Fatal("expected error on empty instance_id")
	}
}

func TestLoadLicenses_FromFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "licenses")
	if err := os.WriteFile(path, []byte("L1:s:h:I1:mt5"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("EXECRELAY_LICENSES_FILE", path)
	t.Setenv("EXECRELAY_LICENSES", "ignored")
	recs, err := LoadLicenses()
	if err != nil || len(recs) != 1 || recs[0].LicenseID != "L1" {
		t.Fatalf("got recs=%+v err=%v", recs, err)
	}
}

func TestLoadLicenses_FromEnv(t *testing.T) {
	t.Setenv("EXECRELAY_LICENSES_FILE", "")
	t.Setenv("EXECRELAY_LICENSES", "L1:s:h:I1")
	recs, err := LoadLicenses()
	if err != nil || len(recs) != 1 {
		t.Fatalf("got recs=%+v err=%v", recs, err)
	}
}

func TestConfigFromEnv_HappyPath(t *testing.T) {
	t.Setenv("HTTP_ADDR", ":9090")
	t.Setenv("NATS_URL", "nats://x:4222")
	t.Setenv("INGRESS_REGION", "test")
	t.Setenv("MAX_BODY_BYTES", "8192")
	t.Setenv("WEBHOOK_TIMESTAMP_WINDOW_SECS", "30")
	t.Setenv("WEBHOOK_RATE_LIMIT", "100")
	t.Setenv("WEBHOOK_ALLOWED_CIDRS", "10.0.0.0/8, 192.168.0.0/16")
	t.Setenv("INGRESS_PERIMETER_TOKEN", "tok")
	t.Setenv("INGRESS_TRADING_HALTED", "true")
	t.Setenv("DEBUG", "false")
	t.Setenv("EXECRELAY_LICENSES", "L1:s:h:I1")
	t.Setenv("EXECRELAY_LICENSES_FILE", "")
	cfg, err := ConfigFromEnv()
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if cfg.HTTPAddr != ":9090" {
		t.Errorf("HTTPAddr: %q", cfg.HTTPAddr)
	}
	if cfg.MaxBodyBytes != 8192 {
		t.Errorf("MaxBodyBytes: %d", cfg.MaxBodyBytes)
	}
	if cfg.TimestampWindow != 30*time.Second {
		t.Errorf("TimestampWindow: %v", cfg.TimestampWindow)
	}
	if cfg.RateLimit != 100 {
		t.Errorf("RateLimit: %d", cfg.RateLimit)
	}
	if len(cfg.AllowedCIDRs) != 2 {
		t.Errorf("AllowedCIDRs: %d", len(cfg.AllowedCIDRs))
	}
	if cfg.PerimeterToken != "tok" || !cfg.TradingHalted || cfg.Debug {
		t.Errorf("flags wrong: %+v", cfg)
	}
}

func TestConfigFromEnv_RejectsBadValues(t *testing.T) {
	for _, env := range []map[string]string{
		{"MAX_BODY_BYTES": "abc"},
		{"WEBHOOK_TIMESTAMP_WINDOW_SECS": "-1"},
		{"WEBHOOK_RATE_LIMIT": "no"},
		{"WEBHOOK_ALLOWED_CIDRS": "not-a-cidr"},
	} {
		t.Run("bad", func(t *testing.T) {
			for k, v := range env {
				t.Setenv(k, v)
			}
			t.Setenv("EXECRELAY_LICENSES_FILE", "")
			t.Setenv("EXECRELAY_LICENSES", "")
			if _, err := ConfigFromEnv(); err == nil {
				t.Fatalf("expected error for env=%v", env)
			}
		})
	}
}

func TestNewServer_AppliesTimeouts(t *testing.T) {
	cfg := Config{
		HTTPAddr:     ":0",
		ReadTimeout:  2 * time.Second,
		WriteTimeout: 3 * time.Second,
	}
	srv := NewServer(cfg, http.NotFoundHandler())
	if srv.ReadHeaderTimeout != 2*time.Second || srv.WriteTimeout != 3*time.Second {
		t.Fatalf("timeouts not applied: %+v", srv)
	}
}

// ---- license store ----------------------------------------------------------

func TestHotReloadLicenseStore_LookupAndReload(t *testing.T) {
	s := NewHotReloadLicenseStore([]LicenseRecord{
		{LicenseID: "L1", Active: true, HMACSecret: "h"},
	})
	rec, err := s.Lookup(context.Background(), "L1")
	if err != nil || rec.LicenseID != "L1" {
		t.Fatalf("first lookup: rec=%+v err=%v", rec, err)
	}
	// inactive
	s.Reload([]LicenseRecord{{LicenseID: "L1", Active: false, HMACSecret: "h"}})
	if _, err := s.Lookup(context.Background(), "L1"); err != ErrLicenseInactive {
		t.Fatalf("expected ErrLicenseInactive, got %v", err)
	}
	// missing
	if _, err := s.Lookup(context.Background(), "missing"); err != ErrLicenseNotFound {
		t.Fatalf("expected ErrLicenseNotFound, got %v", err)
	}
}

// ---- audit -----------------------------------------------------------------

func TestAuditLicenses_ReportsExpectedIssues(t *testing.T) {
	got := AuditLicenses([]LicenseRecord{
		{LicenseID: "open"},                                            // no_auth
		{LicenseID: "hmac-only", HMACSecret: "h"},                      // no_secret
		{LicenseID: "secret-only", Secret: "s"},                        // no_hmac
		{LicenseID: "rotating", HMACSecret: "h", Secret: "s", PendingHMACSecret: "p"},
	})
	if len(got) != 4 {
		t.Fatalf("expected 4 warnings, got %d (%+v)", len(got), got)
	}
	want := map[string]string{
		"open":        "no_auth",
		"hmac-only":   "no_secret",
		"secret-only": "no_hmac",
		"rotating":    "rotation_active",
	}
	for _, w := range got {
		if want[w.LicenseID] != w.Issue {
			t.Errorf("license %q: got %q, want %q", w.LicenseID, w.Issue, want[w.LicenseID])
		}
	}
}

// ---- counter ---------------------------------------------------------------

func TestDailyCounter_IncrementsPerLicenseAndDay(t *testing.T) {
	dc := newDailyCounter()
	now := time.Date(2026, 5, 28, 12, 0, 0, 0, time.UTC)
	if v := dc.Increment("A", now); v != 1 {
		t.Fatalf("first A: %d", v)
	}
	if v := dc.Increment("A", now); v != 2 {
		t.Fatalf("second A: %d", v)
	}
	if v := dc.Increment("B", now); v != 1 {
		t.Fatalf("first B: %d", v)
	}
	// different day resets
	next := now.Add(24 * time.Hour)
	if v := dc.Increment("A", next); v != 1 {
		t.Fatalf("A next day: %d", v)
	}
}

// ---- rate limiter ----------------------------------------------------------

func TestIPRateLimiter_BurstThenDenies(t *testing.T) {
	rl := newIPRateLimiter(1.0, 2) // 1 token/sec, burst 2
	if !rl.allow("1.1.1.1") {
		t.Fatal("first should allow")
	}
	if !rl.allow("1.1.1.1") {
		t.Fatal("second should allow")
	}
	if rl.allow("1.1.1.1") {
		t.Fatal("third should deny")
	}
	// other IP unaffected
	if !rl.allow("2.2.2.2") {
		t.Fatal("new IP should allow")
	}
}

// ---- health + readyz endpoints -------------------------------------------

func TestHealth_ReturnsOK(t *testing.T) {
	h := NewHandler(Options{})
	rec := httptest.NewRecorder()
	h.health(rec, httptest.NewRequest(http.MethodGet, "/health", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d", rec.Code)
	}
	if !bytes.Contains(rec.Body.Bytes(), []byte("\"status\":\"ok\"")) {
		t.Fatalf("body %s", rec.Body.String())
	}
}

func TestReadyz_OKWhenPublisherHealthy(t *testing.T) {
	h := NewHandler(Options{Publisher: NoopPublisher{}})
	rec := httptest.NewRecorder()
	h.readyz(rec, httptest.NewRequest(http.MethodGet, "/readyz", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d body=%s", rec.Code, rec.Body.String())
	}
}

type unhealthyPub struct{ NoopPublisher }

func (unhealthyPub) Healthy() bool { return false }

func TestReadyz_503WhenPublisherDown(t *testing.T) {
	h := NewHandler(Options{Publisher: unhealthyPub{}})
	rec := httptest.NewRecorder()
	h.readyz(rec, httptest.NewRequest(http.MethodGet, "/readyz", nil))
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("status %d", rec.Code)
	}
}

// ---- killSwitch ------------------------------------------------------------

func TestKillSwitch_DisabledWithoutPerimeterToken(t *testing.T) {
	h := NewHandler(Options{})
	rec := httptest.NewRecorder()
	h.killSwitch(rec, httptest.NewRequest(http.MethodGet, "/admin/kill-switch", nil))
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("status %d", rec.Code)
	}
}

func TestKillSwitch_RejectsBadToken(t *testing.T) {
	h := NewHandler(Options{PerimeterToken: "right"})
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/admin/kill-switch?token=wrong", nil)
	h.killSwitch(rec, req)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("status %d", rec.Code)
	}
}

func TestKillSwitch_GETReturnsCurrentState(t *testing.T) {
	h := NewHandler(Options{PerimeterToken: "tok", TradingHalted: true})
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/admin/kill-switch?token=tok", nil)
	h.killSwitch(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d", rec.Code)
	}
	if !bytes.Contains(rec.Body.Bytes(), []byte("\"halted\":\"true\"")) {
		t.Fatalf("body %s", rec.Body.String())
	}
}

func TestKillSwitch_POSTToggles(t *testing.T) {
	h := NewHandler(Options{PerimeterToken: "tok"})
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/admin/kill-switch?token=tok&state=on", nil)
	h.killSwitch(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d body=%s", rec.Code, rec.Body.String())
	}
	if !h.tradingHalted.Load() {
		t.Fatal("expected tradingHalted=true after POST state=on")
	}
	// toggle off
	rec = httptest.NewRecorder()
	req = httptest.NewRequest(http.MethodPost, "/admin/kill-switch?token=tok&state=off", nil)
	h.killSwitch(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d", rec.Code)
	}
	if h.tradingHalted.Load() {
		t.Fatal("expected tradingHalted=false after POST state=off")
	}
}

func TestKillSwitch_POSTRejectsInvalidState(t *testing.T) {
	h := NewHandler(Options{PerimeterToken: "tok"})
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/admin/kill-switch?token=tok&state=garbage", nil)
	h.killSwitch(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status %d", rec.Code)
	}
}

func TestKillSwitch_RejectsOtherMethods(t *testing.T) {
	h := NewHandler(Options{PerimeterToken: "tok"})
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodDelete, "/admin/kill-switch?token=tok", nil)
	h.killSwitch(rec, req)
	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("status %d", rec.Code)
	}
	if rec.Header().Get("Allow") != "GET, POST" {
		t.Fatalf("Allow header %q", rec.Header().Get("Allow"))
	}
}

// ---- truncateStr / metrics helpers ----------------------------------------

func TestTruncateStr_ShortPasses(t *testing.T) {
	if got := truncateStr("abc", 10); got != "abc" {
		t.Fatalf("got %q", got)
	}
}

func TestTruncateStr_TrimsOversize(t *testing.T) {
	if got := truncateStr("abcdefghij", 4); got != "abcd" {
		t.Fatalf("got %q", got)
	}
}

func TestReportLicenseWarnings_ResetsAndSets(t *testing.T) {
	// Should not panic regardless of registry state.
	ReportLicenseWarnings(nil)
	ReportLicenseWarnings([]LicenseWarning{{LicenseID: "L1", Issue: "no_auth"}})
}

// ---- Shutdown + getenv helpers --------------------------------------------

type slowPublisher struct{}

func (slowPublisher) Publish(context.Context, string, []byte) error { return nil }
func (slowPublisher) Healthy() bool                                 { return true }
func (slowPublisher) Close()                                        { time.Sleep(20 * time.Millisecond) }

func TestShutdown_CompletesWithinDeadline(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
	defer cancel()
	if err := Shutdown(ctx, slowPublisher{}); err != nil {
		t.Fatalf("err: %v", err)
	}
}

type hangPublisher struct{}

func (hangPublisher) Publish(context.Context, string, []byte) error { return nil }
func (hangPublisher) Healthy() bool                                 { return true }
func (hangPublisher) Close()                                        { time.Sleep(2 * time.Second) }

func TestShutdown_ReturnsCtxErrOnTimeout(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel()
	err := Shutdown(ctx, hangPublisher{})
	if err == nil {
		t.Fatal("expected timeout err")
	}
}

// ---- ParseLicenseRecords whitespace handling -------------------------------

func TestParseLicenseRecords_TrimsAndSkipsEmptyEntries(t *testing.T) {
	recs, err := ParseLicenseRecords("  ;;L1:s:h:I1;;  ")
	if err != nil {
		t.Fatal(err)
	}
	if len(recs) != 1 || recs[0].LicenseID != "L1" {
		t.Fatalf("got %+v", recs)
	}
}

// ---- NoopPublisher contract ------------------------------------------------

func TestNoopPublisher_Surface(t *testing.T) {
	var p Publisher = NoopPublisher{}
	if err := p.Publish(context.Background(), "subj", []byte("x")); err != nil {
		t.Fatalf("publish err: %v", err)
	}
	if hp, ok := p.(interface{ Healthy() bool }); !ok || !hp.Healthy() {
		t.Fatal("expected Healthy=true on NoopPublisher")
	}
	p.Close()
}

// ---- recordRequestEvent (eventPublisher nil path) -------------------------

func TestRecordRequestEvent_NoPublisherIsNoOp(t *testing.T) {
	h := &Handler{now: time.Now}
	r := httptest.NewRequest(http.MethodPost, "/webhook", strings.NewReader("body"))
	rec := &webhookRecorder{ResponseWriter: httptest.NewRecorder(), status: http.StatusOK}
	// nil eventPublisher → early return, no panic.
	h.recordRequestEvent(r, rec, &webhookCtx{requestID: "rid"}, "1.1.1.1", time.Now())
}

// capturingPublisher records the last publish for assertions.
type capturingPublisher struct {
	subject string
	data    []byte
}

func (c *capturingPublisher) Publish(_ context.Context, subject string, payload []byte) error {
	c.subject = subject
	c.data = payload
	return nil
}
func (c *capturingPublisher) Healthy() bool { return true }
func (c *capturingPublisher) Close()        {}

func TestRecordRequestEvent_EmitsToEvents(t *testing.T) {
	pub := &capturingPublisher{}
	h := &Handler{now: time.Now, eventPublisher: pub, region: "test"}
	r := httptest.NewRequest(http.MethodPost, "/webhook", strings.NewReader("body"))
	rec := &webhookRecorder{ResponseWriter: httptest.NewRecorder(), status: http.StatusOK}
	h.recordRequestEvent(r, rec, &webhookCtx{requestID: "rid", traceID: "t1", licenseKey: "lic"}, "10.0.0.1", time.Now())
	if pub.subject != "events.ingress.request" {
		t.Fatalf("subject: %q", pub.subject)
	}
	if !bytes.Contains(pub.data, []byte(`"request_id":"rid"`)) {
		t.Fatalf("payload missing request_id: %s", pub.data)
	}
	if !bytes.Contains(pub.data, []byte(`"outcome":"accepted"`)) {
		t.Fatalf("outcome wrong: %s", pub.data)
	}
}

func TestRecordRequestEvent_RejectedOutcomeFor4xx(t *testing.T) {
	pub := &capturingPublisher{}
	h := &Handler{now: time.Now, eventPublisher: pub, region: "test"}
	r := httptest.NewRequest(http.MethodPost, "/webhook", strings.NewReader("body"))
	rec := &webhookRecorder{ResponseWriter: httptest.NewRecorder(), status: http.StatusUnauthorized}
	h.recordRequestEvent(r, rec, &webhookCtx{requestID: "rid", reasonCode: "signature_rejected"}, "10.0.0.1", time.Now())
	if !bytes.Contains(pub.data, []byte(`"outcome":"rejected"`)) {
		t.Fatalf("expected rejected, got %s", pub.data)
	}
}

// ---- ipRateLimiter cleanup loop runs and prunes (smoke) --------------------

func TestIPRateLimiter_NoPanicOnConcurrentAllow(t *testing.T) {
	rl := newIPRateLimiter(100.0, 5)
	done := make(chan struct{}, 4)
	for i := 0; i < 4; i++ {
		go func(id int) {
			for j := 0; j < 50; j++ {
				rl.allow(net.IPv4(byte(id), 0, 0, byte(j)).String())
			}
			done <- struct{}{}
		}(i)
	}
	for i := 0; i < 4; i++ {
		<-done
	}
}

// ---- scoreSignalWithML: exercise the predictor-down fallback path ---------

func TestScoreSignalWithML_FallbackWhenPredictorDown(t *testing.T) {
	// No ml-predictor server running — http.Post returns error → 0.5 fallback.
	h := &Handler{now: time.Now}
	got, err := h.scoreSignalWithML(context.Background(), "EURUSD", time.Now().Unix())
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if got != 0.5 {
		t.Fatalf("expected fallback 0.5, got %f", got)
	}
}

// ---- checkExposureLimits: nil db path is the cheap one --------------------

func TestCheckExposureLimits_NilDBAllowsThrough(t *testing.T) {
	h := &Handler{now: time.Now}
	r := h.checkExposureLimits(context.Background(), "L1", "A1", nil)
	if !r.AllowedToProceed {
		t.Fatal("nil DB should allow signal through")
	}
}

// ---- NatsPublisher zero-value safety --------------------------------------

func TestNatsPublisher_NilSafeOps(t *testing.T) {
	var p *NatsPublisher
	if p.Healthy() {
		t.Fatal("nil publisher must report unhealthy")
	}
	if err := p.Publish(context.Background(), "x", []byte("y")); err == nil {
		t.Fatal("nil publisher should fail to publish")
	}
	p.Close() // must not panic
}

// ---- newIPRateLimiter: returns non-nil with correct burst/rate -----------

func TestNewIPRateLimiter_RecordsBurstAndRate(t *testing.T) {
	rl := newIPRateLimiter(7.5, 4)
	if rl.burst != 4 || rl.rate != 7.5 {
		t.Fatalf("rl: %+v", rl)
	}
}

// ---- webhook: additional scenarios for branch coverage -------------------

func TestWebhook_IPAllowedByCIDR(t *testing.T) {
	_, cidr, _ := net.ParseCIDR("10.0.0.0/8")
	pub := &capturingPublisher{}
	h := NewHandler(Options{
		Store: NewStaticLicenseStore([]LicenseRecord{
			{LicenseID: "L1", InstanceID: "I1", Platform: "mt5", Active: true, HMACSecret: "sec"},
		}),
		Publisher:      pub,
		EventPublisher: pub,
		AllowedCIDRs:   []*net.IPNet{cidr},
	})
	body := "L1,buy,I1,vol_lots=0.1"
	req := httptest.NewRequest(http.MethodPost, "/webhook", strings.NewReader(body))
	req.RemoteAddr = "10.0.0.5:5555"
	addValidHMAC(req, []byte(body), "sec")
	rr := httptest.NewRecorder()
	h.webhook(rr, req)
	if rr.Code != http.StatusOK {
		t.Fatalf("status %d body=%s", rr.Code, rr.Body.String())
	}
}

func TestWebhook_BodyTooLargeReturns413(t *testing.T) {
	h := NewHandler(Options{
		Store:        NewStaticLicenseStore([]LicenseRecord{{LicenseID: "L1", InstanceID: "I1", Platform: "mt5", Active: true}}),
		Publisher:    NoopPublisher{},
		MaxBodyBytes: 8, // tiny — anything real exceeds it
	})
	body := strings.Repeat("X", 64)
	req := httptest.NewRequest(http.MethodPost, "/webhook", strings.NewReader(body))
	rr := httptest.NewRecorder()
	h.webhook(rr, req)
	if rr.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("status %d", rr.Code)
	}
}

func TestWebhook_PlanLimitEnforced(t *testing.T) {
	pub := &capturingPublisher{}
	h := NewHandler(Options{
		Store: NewStaticLicenseStore([]LicenseRecord{
			{LicenseID: "L1", InstanceID: "I1", Platform: "mt5", Active: true,
				HMACSecret: "sec", MaxSignalsPerDay: 1},
		}),
		Publisher:      pub,
		EventPublisher: pub,
	})
	body := "L1,buy,I1,vol_lots=0.1"

	for i, wantStatus := range []int{http.StatusOK, http.StatusTooManyRequests} {
		req := httptest.NewRequest(http.MethodPost, "/webhook", strings.NewReader(body))
		req.RemoteAddr = "1.2.3.4:5555"
		addValidHMAC(req, []byte(body), "sec")
		rr := httptest.NewRecorder()
		h.webhook(rr, req)
		if rr.Code != wantStatus {
			t.Fatalf("attempt %d: status %d want %d body=%s", i+1, rr.Code, wantStatus, rr.Body.String())
		}
	}
}

func TestWebhook_RespectsIncomingTraceID(t *testing.T) {
	pub := &capturingPublisher{}
	h := NewHandler(Options{
		Store: NewStaticLicenseStore([]LicenseRecord{
			{LicenseID: "L1", InstanceID: "I1", Platform: "mt5", Active: true, HMACSecret: "sec"},
		}),
		Publisher:      pub,
		EventPublisher: pub,
	})
	body := "L1,buy,I1,vol_lots=0.1"
	req := httptest.NewRequest(http.MethodPost, "/webhook", strings.NewReader(body))
	req.Header.Set("X-ExecRelay-Trace-ID", "caller-trace-1234")
	addValidHMAC(req, []byte(body), "sec")
	rr := httptest.NewRecorder()
	h.webhook(rr, req)
	if rr.Code != http.StatusOK {
		t.Fatalf("status %d", rr.Code)
	}
	if !bytes.Contains(rr.Body.Bytes(), []byte("caller-trace-1234")) {
		t.Fatalf("trace id missing from response body: %s", rr.Body.String())
	}
	if rr.Header().Get("X-ExecRelay-Trace-ID") != "caller-trace-1234" {
		t.Fatalf("response trace header missing")
	}
}

// addValidHMAC computes the test signature the handler expects.
func addValidHMAC(r *http.Request, body []byte, secret string) {
	mac := hmacNew(secret)
	mac.Write(body)
	r.Header.Set("X-ExecRelay-Signature", "sha256="+hexEncode(mac.Sum(nil)))
}
