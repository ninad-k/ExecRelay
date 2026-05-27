package ingress

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"net/http"
	"net/http/httptest"
	"strconv"
	"testing"
	"time"

	oldproto "github.com/golang/protobuf/proto"
	execrelaypb "github.com/ninadk/execrelay/packages/proto"
)

type capturePublisher struct {
	subject string
	payload []byte
	err     error
}

func (p *capturePublisher) Publish(_ context.Context, subject string, payload []byte) error {
	p.subject = subject
	p.payload = append(p.payload[:0], payload...)
	return p.err
}

func (p *capturePublisher) Close() {}

func TestWebhookPublishesSignal(t *testing.T) {
	publisher := &capturePublisher{}
	handler := testHandler(publisher).Routes()
	body := "60123456789,buy,EURUSD,vol_lots=0.1,sl_pips=20,tp_pips=40,secret=alert-secret"

	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewBufferString(body))
	req.Header.Set("X-ExecRelay-Signature", signature(body, "hmac-secret"))
	rr := httptest.NewRecorder()

	handler.ServeHTTP(rr, req)
	if rr.Code != http.StatusOK {
		t.Fatalf("status = %d body = %s", rr.Code, rr.Body.String())
	}
	if publisher.subject != "signals.mt5.60123456789.mt5-a" {
		t.Fatalf("subject = %q", publisher.subject)
	}

	var signal execrelaypb.Signal
	if err := oldproto.Unmarshal(publisher.payload, &signal); err != nil {
		t.Fatalf("unmarshal payload: %v", err)
	}
	if signal.TraceId == "" || signal.LicenseId != "60123456789" || signal.Command != "buy" || signal.Symbol != "EURUSD" {
		t.Fatalf("unexpected signal = %#v", signal)
	}
	for _, param := range signal.Params {
		if param.Key == "secret" {
			t.Fatal("secret leaked into protobuf params")
		}
	}
}

func TestWebhookRejectsBadSecret(t *testing.T) {
	handler := testHandler(&capturePublisher{}).Routes()
	body := "60123456789,buy,EURUSD,vol_lots=0.1,sl_pips=20,secret=wrong"
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewBufferString(body))
	req.Header.Set("X-ExecRelay-Signature", signature(body, "hmac-secret"))
	rr := httptest.NewRecorder()

	handler.ServeHTTP(rr, req)
	if rr.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d body = %s", rr.Code, rr.Body.String())
	}
}

func TestWebhookRejectsBadSignature(t *testing.T) {
	handler := testHandler(&capturePublisher{}).Routes()
	body := "60123456789,buy,EURUSD,vol_lots=0.1,sl_pips=20,secret=alert-secret"
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewBufferString(body))
	req.Header.Set("X-ExecRelay-Signature", signature(body, "wrong"))
	rr := httptest.NewRecorder()

	handler.ServeHTTP(rr, req)
	if rr.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d body = %s", rr.Code, rr.Body.String())
	}
}

func TestWebhookRejectsUnknownLicense(t *testing.T) {
	handler := testHandler(&capturePublisher{}).Routes()
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewBufferString("999,buy,EURUSD,vol_lots=0.1,sl_pips=20"))
	rr := httptest.NewRecorder()

	handler.ServeHTTP(rr, req)
	if rr.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d body = %s", rr.Code, rr.Body.String())
	}
}

func TestWebhookRejectsParseError(t *testing.T) {
	handler := testHandler(&capturePublisher{}).Routes()
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewBufferString("60123456789,buy,EURUSD"))
	rr := httptest.NewRecorder()

	handler.ServeHTTP(rr, req)
	if rr.Code != http.StatusBadRequest {
		t.Fatalf("status = %d body = %s", rr.Code, rr.Body.String())
	}
}

func TestWebhookPublishFailure(t *testing.T) {
	handler := testHandler(&capturePublisher{err: errors.New("nats down")}).Routes()
	body := "60123456789,buy,EURUSD,vol_lots=0.1,sl_pips=20,secret=alert-secret"
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewBufferString(body))
	req.Header.Set("X-ExecRelay-Signature", signature(body, "hmac-secret"))
	rr := httptest.NewRecorder()

	handler.ServeHTTP(rr, req)
	if rr.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d body = %s", rr.Code, rr.Body.String())
	}
}

func TestWebhookMethod(t *testing.T) {
	handler := testHandler(&capturePublisher{}).Routes()
	req := httptest.NewRequest(http.MethodGet, "/webhook", nil)
	rr := httptest.NewRecorder()

	handler.ServeHTTP(rr, req)
	if rr.Code != http.StatusMethodNotAllowed {
		t.Fatalf("status = %d", rr.Code)
	}
}

func BenchmarkWebhook(b *testing.B) {
	publisher := &capturePublisher{}
	handler := testHandler(publisher).Routes()
	body := "60123456789,buy,EURUSD,vol_lots=0.1,sl_pips=20,tp_pips=40,secret=alert-secret"
	sig := signature(body, "hmac-secret")

	b.ReportAllocs()
	for i := 0; i < b.N; i++ {
		req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewBufferString(body))
		req.Header.Set("X-ExecRelay-Signature", sig)
		rr := httptest.NewRecorder()
		handler.ServeHTTP(rr, req)
		if rr.Code != http.StatusOK {
			b.Fatalf("status = %d", rr.Code)
		}
	}
}

func TestWebhookTimestampAccepted(t *testing.T) {
	now := time.Unix(1700000000, 0)
	handler := NewHandler(Options{
		Store: NewStaticLicenseStore([]LicenseRecord{{
			LicenseID: "60123456789", Secret: "alert-secret",
			HMACSecret: "hmac-secret", InstanceID: "mt5-a", Active: true,
		}}),
		Publisher: &capturePublisher{}, Region: "iad", MaxBodyBytes: 1024,
		Now: func() time.Time { return now }, TimestampWindow: 30 * time.Second,
	})
	body := "60123456789,buy,EURUSD,vol_lots=0.1,sl_pips=20,secret=alert-secret"
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewBufferString(body))
	req.Header.Set("X-ExecRelay-Signature", signature(body, "hmac-secret"))
	req.Header.Set("X-ExecRelay-Timestamp", strconv.FormatInt(now.Unix(), 10))
	rr := httptest.NewRecorder()
	handler.Routes().ServeHTTP(rr, req)
	if rr.Code != http.StatusOK {
		t.Fatalf("status = %d body = %s", rr.Code, rr.Body.String())
	}
}

func TestWebhookTimestampRejectedStale(t *testing.T) {
	now := time.Unix(1700000000, 0)
	handler := NewHandler(Options{
		Store: NewStaticLicenseStore([]LicenseRecord{{
			LicenseID: "60123456789", Secret: "alert-secret",
			HMACSecret: "hmac-secret", InstanceID: "mt5-a", Active: true,
		}}),
		Publisher: &capturePublisher{}, Region: "iad", MaxBodyBytes: 1024,
		Now: func() time.Time { return now }, TimestampWindow: 30 * time.Second,
	})
	body := "60123456789,buy,EURUSD,vol_lots=0.1,sl_pips=20,secret=alert-secret"
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewBufferString(body))
	req.Header.Set("X-ExecRelay-Signature", signature(body, "hmac-secret"))
	req.Header.Set("X-ExecRelay-Timestamp", strconv.FormatInt(now.Unix()-60, 10))
	rr := httptest.NewRecorder()
	handler.Routes().ServeHTTP(rr, req)
	if rr.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401 for stale timestamp, got %d", rr.Code)
	}
}

func testHandler(publisher Publisher) *Handler {
	return NewHandler(Options{
		Store: NewStaticLicenseStore([]LicenseRecord{{
			LicenseID:  "60123456789",
			Secret:     "alert-secret",
			HMACSecret: "hmac-secret",
			InstanceID: "mt5-a",
			Active:     true,
		}}),
		Publisher:    publisher,
		Region:       "iad",
		MaxBodyBytes: 1024,
		Now: func() time.Time {
			return time.Unix(1700000000, 123)
		},
	})
}

func TestWebhookPerimeterGateAcceptsValidToken(t *testing.T) {
	publisher := &capturePublisher{}
	handler := perimeterHandler(publisher, "gate-secret").Routes()
	body := "60123456789,buy,EURUSD,vol_lots=0.1,sl_pips=20,tp_pips=40,secret=alert-secret"

	req := httptest.NewRequest(http.MethodPost, "/webhook?token=gate-secret", bytes.NewBufferString(body))
	req.Header.Set("X-ExecRelay-Signature", signature(body, "hmac-secret"))
	rr := httptest.NewRecorder()

	handler.ServeHTTP(rr, req)
	if rr.Code != http.StatusOK {
		t.Fatalf("status = %d body = %s", rr.Code, rr.Body.String())
	}
}

func TestWebhookPerimeterGateRejectsMissingToken(t *testing.T) {
	handler := perimeterHandler(&capturePublisher{}, "gate-secret").Routes()
	body := "60123456789,buy,EURUSD,vol_lots=0.1,secret=alert-secret"

	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewBufferString(body))
	rr := httptest.NewRecorder()

	handler.ServeHTTP(rr, req)
	if rr.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d body = %s", rr.Code, rr.Body.String())
	}
	if !bytes.Contains(rr.Body.Bytes(), []byte("perimeter_rejected")) {
		t.Fatalf("expected perimeter_rejected error, got %s", rr.Body.String())
	}
}

func TestWebhookPerimeterGateRejectsWrongToken(t *testing.T) {
	handler := perimeterHandler(&capturePublisher{}, "gate-secret").Routes()
	body := "60123456789,buy,EURUSD,vol_lots=0.1,secret=alert-secret"

	req := httptest.NewRequest(http.MethodPost, "/webhook?token=wrong", bytes.NewBufferString(body))
	rr := httptest.NewRecorder()

	handler.ServeHTTP(rr, req)
	if rr.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d body = %s", rr.Code, rr.Body.String())
	}
}

func TestWebhookPerimeterGateDisabledWhenEmpty(t *testing.T) {
	// When PerimeterToken is empty, the gate is off and requests without
	// ?token= must still succeed if the per-license auth passes.
	handler := testHandler(&capturePublisher{}).Routes()
	body := "60123456789,buy,EURUSD,vol_lots=0.1,secret=alert-secret"

	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewBufferString(body))
	req.Header.Set("X-ExecRelay-Signature", signature(body, "hmac-secret"))
	rr := httptest.NewRecorder()

	handler.ServeHTTP(rr, req)
	if rr.Code != http.StatusOK {
		t.Fatalf("status = %d body = %s", rr.Code, rr.Body.String())
	}
}

func TestKillSwitchHaltedRejectsWebhook(t *testing.T) {
	h := perimeterHandler(&capturePublisher{}, "gate-secret")
	h.tradingHalted.Store(true)
	handler := h.Routes()
	body := "60123456789,buy,EURUSD,vol_lots=0.1,secret=alert-secret"

	req := httptest.NewRequest(http.MethodPost, "/webhook?token=gate-secret", bytes.NewBufferString(body))
	req.Header.Set("X-ExecRelay-Signature", signature(body, "hmac-secret"))
	rr := httptest.NewRecorder()

	handler.ServeHTTP(rr, req)
	if rr.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d body = %s", rr.Code, rr.Body.String())
	}
	if !bytes.Contains(rr.Body.Bytes(), []byte("trading_halted")) {
		t.Fatalf("expected trading_halted, got %s", rr.Body.String())
	}
}

func TestKillSwitchEndpointGetReturnsCurrentState(t *testing.T) {
	h := perimeterHandler(&capturePublisher{}, "gate-secret")
	handler := h.Routes()

	req := httptest.NewRequest(http.MethodGet, "/admin/kill-switch?token=gate-secret", nil)
	rr := httptest.NewRecorder()
	handler.ServeHTTP(rr, req)
	if rr.Code != http.StatusOK {
		t.Fatalf("status = %d body = %s", rr.Code, rr.Body.String())
	}
	if !bytes.Contains(rr.Body.Bytes(), []byte(`"halted":"false"`)) {
		t.Fatalf("expected halted=false, got %s", rr.Body.String())
	}
}

func TestKillSwitchEndpointTogglesOnAndOff(t *testing.T) {
	h := perimeterHandler(&capturePublisher{}, "gate-secret")
	handler := h.Routes()

	// Turn it ON
	req := httptest.NewRequest(http.MethodPost, "/admin/kill-switch?token=gate-secret&state=on", nil)
	rr := httptest.NewRecorder()
	handler.ServeHTTP(rr, req)
	if rr.Code != http.StatusOK {
		t.Fatalf("on: status = %d body = %s", rr.Code, rr.Body.String())
	}
	if !h.tradingHalted.Load() {
		t.Fatal("expected tradingHalted=true after state=on")
	}

	// Turn it OFF
	req = httptest.NewRequest(http.MethodPost, "/admin/kill-switch?token=gate-secret&state=off", nil)
	rr = httptest.NewRecorder()
	handler.ServeHTTP(rr, req)
	if rr.Code != http.StatusOK {
		t.Fatalf("off: status = %d body = %s", rr.Code, rr.Body.String())
	}
	if h.tradingHalted.Load() {
		t.Fatal("expected tradingHalted=false after state=off")
	}
}

func TestKillSwitchEndpointRejectsBadToken(t *testing.T) {
	h := perimeterHandler(&capturePublisher{}, "gate-secret")
	handler := h.Routes()

	req := httptest.NewRequest(http.MethodPost, "/admin/kill-switch?token=wrong&state=on", nil)
	rr := httptest.NewRecorder()
	handler.ServeHTTP(rr, req)
	if rr.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d body = %s", rr.Code, rr.Body.String())
	}
	if h.tradingHalted.Load() {
		t.Fatal("kill switch toggled despite bad token")
	}
}

func TestKillSwitchEndpointRejectsBadState(t *testing.T) {
	h := perimeterHandler(&capturePublisher{}, "gate-secret")
	handler := h.Routes()

	req := httptest.NewRequest(http.MethodPost, "/admin/kill-switch?token=gate-secret&state=maybe", nil)
	rr := httptest.NewRecorder()
	handler.ServeHTTP(rr, req)
	if rr.Code != http.StatusBadRequest {
		t.Fatalf("status = %d body = %s", rr.Code, rr.Body.String())
	}
}

func TestKillSwitchEndpointDisabledWhenNoPerimeterToken(t *testing.T) {
	// Without INGRESS_PERIMETER_TOKEN configured, the endpoint refuses to act
	// so a wide-open ingress can't be toggled by anyone on the network.
	handler := testHandler(&capturePublisher{}).Routes()

	req := httptest.NewRequest(http.MethodPost, "/admin/kill-switch?state=on", nil)
	rr := httptest.NewRecorder()
	handler.ServeHTTP(rr, req)
	if rr.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d body = %s", rr.Code, rr.Body.String())
	}
	if !bytes.Contains(rr.Body.Bytes(), []byte("kill_switch_disabled")) {
		t.Fatalf("expected kill_switch_disabled, got %s", rr.Body.String())
	}
}

func TestKillSwitchEnvVarStartsHalted(t *testing.T) {
	// Verifies TradingHalted option initialises the in-memory flag.
	h := NewHandler(Options{
		Store: NewStaticLicenseStore([]LicenseRecord{{
			LicenseID:  "60123456789",
			Secret:     "alert-secret",
			HMACSecret: "hmac-secret",
			InstanceID: "mt5-a",
			Active:     true,
		}}),
		Publisher:     &capturePublisher{},
		Region:        "iad",
		MaxBodyBytes:  1024,
		TradingHalted: true,
		Now: func() time.Time {
			return time.Unix(1700000000, 123)
		},
	})
	if !h.tradingHalted.Load() {
		t.Fatal("Options.TradingHalted=true did not initialise handler state")
	}
}

func perimeterHandler(publisher Publisher, token string) *Handler {
	return NewHandler(Options{
		Store: NewStaticLicenseStore([]LicenseRecord{{
			LicenseID:  "60123456789",
			Secret:     "alert-secret",
			HMACSecret: "hmac-secret",
			InstanceID: "mt5-a",
			Active:     true,
		}}),
		Publisher:      publisher,
		Region:         "iad",
		MaxBodyBytes:   1024,
		PerimeterToken: token,
		Now: func() time.Time {
			return time.Unix(1700000000, 123)
		},
	})
}

func signature(body, secret string) string {
	mac := hmac.New(sha256.New, []byte(secret))
	_, _ = mac.Write([]byte(body))
	return "sha256=" + hex.EncodeToString(mac.Sum(nil))
}
