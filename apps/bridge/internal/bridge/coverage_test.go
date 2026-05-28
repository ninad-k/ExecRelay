package bridge

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

// These tests round out coverage on the non-WebSocket helper surface:
// config parsing, hub heartbeat/zombie detection, health endpoints, and
// the small helpers (constantEqual, getenv). The WebSocket flow stays
// covered by handler_test.go.

// ---- config ---------------------------------------------------------------

func TestConfigFromEnv_AppliesEnvOverrides(t *testing.T) {
	t.Setenv("HTTP_ADDR", ":9090")
	t.Setenv("NATS_URL", "nats://x:4222")
	t.Setenv("BRIDGE_REGION", "test")
	t.Setenv("SIGNALS_STREAM", "STR")
	t.Setenv("SIGNALS_CONSUMER", "cons")
	t.Setenv("BRIDGE_AUTH_TOKEN", "tok")
	cfg := ConfigFromEnv()
	if cfg.HTTPAddr != ":9090" || cfg.NATSURL != "nats://x:4222" {
		t.Fatalf("env not applied: %+v", cfg)
	}
	if cfg.Region != "test" || cfg.StreamName != "STR" || cfg.ConsumerName != "cons" {
		t.Fatalf("env not applied: %+v", cfg)
	}
	if cfg.AuthToken != "tok" {
		t.Fatalf("AuthToken: %q", cfg.AuthToken)
	}
}

func TestConfigFromEnv_FallsBackToDefaults(t *testing.T) {
	t.Setenv("HTTP_ADDR", "")
	t.Setenv("NATS_URL", "")
	t.Setenv("BRIDGE_REGION", "")
	t.Setenv("SIGNALS_STREAM", "")
	t.Setenv("SIGNALS_CONSUMER", "")
	t.Setenv("BRIDGE_AUTH_TOKEN", "")
	cfg := ConfigFromEnv()
	if cfg.HTTPAddr != ":8080" || cfg.NATSURL != "nats://nats:4222" {
		t.Fatalf("defaults not applied: %+v", cfg)
	}
}

// ---- constructors --------------------------------------------------------

func TestNewHandler_Constructors(t *testing.T) {
	hub := NewHub()
	h := NewHandler(hub)
	if h.hub != hub {
		t.Fatal("NewHandler did not retain hub")
	}
	h2 := NewHandlerWithPublisher(hub, noopPub{})
	if h2.publisher == nil {
		t.Fatal("NewHandlerWithPublisher did not set publisher")
	}
	h3 := NewHandlerWithAuth(hub, nil, "tok")
	if h3.authToken != "tok" {
		t.Fatal("NewHandlerWithAuth did not set token")
	}
}

type noopPub struct{}

func (noopPub) Publish(string, []byte) error { return nil }

// ---- health / readyz -----------------------------------------------------

func TestHealth_Returns200_NoNATS(t *testing.T) {
	h := NewHandler(NewHub())
	rr := httptest.NewRecorder()
	h.health(rr, httptest.NewRequest(http.MethodGet, "/healthz", nil))
	if rr.Code != http.StatusOK {
		t.Fatalf("status %d body %s", rr.Code, rr.Body.String())
	}
}

func TestReadyz_OKWhenNATSAbsent(t *testing.T) {
	h := NewHandler(NewHub())
	rr := httptest.NewRecorder()
	h.readyz(rr, httptest.NewRequest(http.MethodGet, "/readyz", nil))
	if rr.Code != http.StatusOK {
		t.Fatalf("status %d body %s", rr.Code, rr.Body.String())
	}
	var body map[string]any
	_ = json.Unmarshal(rr.Body.Bytes(), &body)
	if body["ok"] != true {
		t.Fatalf("ok=%v body=%v", body["ok"], body)
	}
}

// ---- Hub / Conn ----------------------------------------------------------

type fakeWS struct {
	written  []any
	writeErr error
	closed   bool
}

func (f *fakeWS) WriteJSON(v any) error {
	if f.writeErr != nil {
		return f.writeErr
	}
	f.written = append(f.written, v)
	return nil
}
func (f *fakeWS) Close() error { f.closed = true; return nil }

func TestConn_WriteJSONSerializes(t *testing.T) {
	ws := &fakeWS{}
	c := NewConn(ws, "I1")
	if err := c.WriteJSON("hi"); err != nil {
		t.Fatal(err)
	}
	if len(ws.written) != 1 || ws.written[0] != "hi" {
		t.Fatalf("ws.written=%v", ws.written)
	}
}

func TestConn_WriteJSONPropagatesError(t *testing.T) {
	ws := &fakeWS{writeErr: errors.New("simulated")}
	c := NewConn(ws, "I1")
	if err := c.WriteJSON("hi"); err == nil {
		t.Fatal("expected error")
	}
}

func TestConn_HeartbeatAndZombie(t *testing.T) {
	c := NewConn(&fakeWS{}, "I1")
	if c.IsZombie(time.Millisecond) {
		t.Fatal("brand-new conn must not be zombie (no heartbeat yet)")
	}
	c.UpdateHeartbeat()
	if c.IsZombie(time.Hour) {
		t.Fatal("just-beaten heartbeat must not be zombie under hour threshold")
	}
	c.lastHeartbeat = time.Now().Add(-2 * time.Hour)
	if !c.IsZombie(time.Hour) {
		t.Fatal("old heartbeat must register as zombie")
	}
}

func TestHub_RegisterAndUnregister(t *testing.T) {
	hub := NewHub()
	c := NewConn(&fakeWS{}, "I1")
	hub.Register(c)
	got, ok := hub.Get("I1")
	if !ok || got != c {
		t.Fatal("conn not retrievable after register")
	}
	hub.Unregister(c)
	if _, ok := hub.Get("I1"); ok {
		t.Fatal("conn still registered after unregister")
	}
}

func TestHub_RegisterReplacesOldConn(t *testing.T) {
	hub := NewHub()
	old := NewConn(&fakeWS{}, "I1")
	hub.Register(old)
	new1 := NewConn(&fakeWS{}, "I1")
	hub.Register(new1)
	// The old conn should have been closed.
	if !old.ws.(*fakeWS).closed {
		t.Fatal("old conn not closed on duplicate register")
	}
	got, _ := hub.Get("I1")
	if got != new1 {
		t.Fatal("hub did not replace conn on re-register")
	}
}

func TestHub_UnregisterIgnoresStaleConn(t *testing.T) {
	hub := NewHub()
	c1 := NewConn(&fakeWS{}, "I1")
	hub.Register(c1)
	c2 := NewConn(&fakeWS{}, "I1")
	hub.Register(c2)
	// Unregistering the original (now-replaced) conn must not pull c2 out.
	hub.Unregister(c1)
	if got, _ := hub.Get("I1"); got != c2 {
		t.Fatal("unregister of stale conn evicted the replacement")
	}
}

// ---- helpers --------------------------------------------------------------

func TestConstantEqual(t *testing.T) {
	if !constantEqual("abc", "abc") {
		t.Fatal("equal strings should match")
	}
	if constantEqual("abc", "abd") {
		t.Fatal("different strings should not match")
	}
	if constantEqual("ab", "abc") {
		t.Fatal("different lengths should not match")
	}
}

func TestSetConsumerLag_NoPanic(t *testing.T) {
	SetConsumerLag("test-stream", 5)
	SetConsumerLag("test-stream", 0)
}
