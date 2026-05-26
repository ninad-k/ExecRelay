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

func signature(body, secret string) string {
	mac := hmac.New(sha256.New, []byte(secret))
	_, _ = mac.Write([]byte(body))
	return "sha256=" + hex.EncodeToString(mac.Sum(nil))
}
