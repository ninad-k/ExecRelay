package dxtrade

import (
	"context"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
)

// Tests covering the testable subset of dxtrade: config parsing, metrics
// helpers, baseURL, error stringification, and the placeOrder happy path
// against a stub HTTP server. The REST client paths that need a real broker
// session are exercised end-to-end in integration tests, not here.

// ---- config --------------------------------------------------------------

func TestConfigFromEnv_DefaultsAndOverrides(t *testing.T) {
	t.Setenv("HTTP_ADDR", "")
	t.Setenv("NATS_URL", "")
	t.Setenv("DXTRADE_REGION", "")
	t.Setenv("SIGNALS_STREAM", "")
	t.Setenv("SIGNALS_CONSUMER", "")
	t.Setenv("DXTRADE_INSTANCES", "")
	cfg, err := ConfigFromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.HTTPAddr != ":8080" || cfg.NATSURL != "nats://nats:4222" {
		t.Fatalf("defaults wrong: %+v", cfg)
	}

	t.Setenv("HTTP_ADDR", ":9091")
	t.Setenv("NATS_URL", "nats://x:4222")
	t.Setenv("DXTRADE_REGION", "test")
	t.Setenv("SIGNALS_STREAM", "STR")
	t.Setenv("SIGNALS_CONSUMER", "cons")
	t.Setenv("DXTRADE_INSTANCES", "I1:demo.example.com:user:pw:ACC1")
	cfg, err = ConfigFromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.HTTPAddr != ":9091" || cfg.Region != "test" {
		t.Fatalf("env overrides not applied: %+v", cfg)
	}
	if len(cfg.Instances) != 1 || cfg.Instances[0].InstanceID != "I1" {
		t.Fatalf("instances: %+v", cfg.Instances)
	}
}

func TestConfigFromEnv_PropagatesInstanceParseError(t *testing.T) {
	t.Setenv("DXTRADE_INSTANCES", "not-enough-fields")
	if _, err := ConfigFromEnv(); err == nil {
		t.Fatal("expected error for malformed DXTRADE_INSTANCES")
	}
}

// ---- metrics --------------------------------------------------------------

func TestMetricHelpers_NoPanic(t *testing.T) {
	RecordCommandProcessed("buy")
	RecordExecutionLatency(0.0125)
	RecordCircuitBreakerTrip("I1")
	RecordBrokerFailure("I1", "timeout")
}

// ---- baseURL + authError --------------------------------------------------

func TestBaseURL_IncludesHost(t *testing.T) {
	c := NewClient(InstanceConfig{InstanceID: "I1", Host: "demo.x.com"})
	if got := c.baseURL(); got != "https://demo.x.com" {
		t.Fatalf("baseURL = %q", got)
	}
}

func TestAuthError_Message(t *testing.T) {
	if !strings.Contains(authError{}.Error(), "auth") {
		t.Fatalf("authError.Error() = %v", authError{}.Error())
	}
}

// ---- Execute: unsupported action short-circuits before HTTP --------------

func TestExecute_UnsupportedActionRejected(t *testing.T) {
	c := NewClient(InstanceConfig{InstanceID: "I1", Host: "demo.x.com"})
	c.token = "skip-login" // bypass login attempt against a non-existent host
	_, err := c.Execute(context.Background(), &Command{Action: "FROOBAR"})
	if err == nil {
		t.Fatal("expected unsupported-action error")
	}
}

// ---- Execute: full happy path against an httptest stub -------------------

// httpRewriter forces every outbound request to point at the stub server
// regardless of the URL the client builds. Lets us reuse baseURL()'s
// "https://<Host>" without standing up a TLS endpoint.
type httpRewriter struct {
	target string // e.g. http://127.0.0.1:PORT
	inner  http.RoundTripper
}

func (r httpRewriter) RoundTrip(req *http.Request) (*http.Response, error) {
	target, _ := url.Parse(r.target)
	req2 := req.Clone(req.Context())
	req2.URL.Scheme = target.Scheme
	req2.URL.Host = target.Host
	req2.Host = target.Host
	tr := r.inner
	if tr == nil {
		tr = http.DefaultTransport
	}
	return tr.RoundTrip(req2)
}

func TestExecute_PlaceOrder_HappyPath(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/auth/login":
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"sessionToken":"tok-abc"}`))
		default:
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"orderId":"O-1","status":"FILLED"}`))
		}
	}))
	defer srv.Close()

	c := NewClient(InstanceConfig{
		InstanceID: "I1",
		Host:       "demo.x.com",
		Username:   "u",
		Password:   "p",
		Account:    "ACC1",
	})
	c.http.Transport = httpRewriter{target: srv.URL}

	res, err := c.Execute(context.Background(), &Command{
		Action: ActionBuy,
		Symbol: "EURUSD",
		Volume: 0.1,
	})
	if err != nil {
		t.Fatalf("execute: %v", err)
	}
	if res == nil || res.BrokerOrderID == "" {
		t.Fatalf("expected non-empty result: %+v", res)
	}
}

func TestExecute_LoginFailureBubblesUp(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer srv.Close()

	c := NewClient(InstanceConfig{
		InstanceID: "I1",
		Host:       "demo.x.com",
		Username:   "u",
		Password:   "p",
	})
	c.http.Transport = httpRewriter{target: srv.URL}

	if _, err := c.Execute(context.Background(), &Command{Action: ActionBuy, Symbol: "EURUSD", Volume: 0.1}); err == nil {
		t.Fatal("expected execute error when login returns 500")
	}
}

func newAuthedClient(t *testing.T, handler http.HandlerFunc) (*Client, func()) {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/auth/login" {
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"sessionToken":"tok"}`))
			return
		}
		handler(w, r)
	}))
	c := NewClient(InstanceConfig{
		InstanceID: "I1", Host: "demo.x.com", Username: "u", Password: "p", Account: "ACC1",
	})
	c.http.Transport = httpRewriter{target: srv.URL}
	return c, srv.Close
}

func TestExecute_ClosePositions(t *testing.T) {
	c, done := newAuthedClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet {
			_, _ = w.Write([]byte(`[{"symbol":"EURUSD","side":"BUY","positionId":"P1"},{"symbol":"GBPUSD","side":"BUY","positionId":"P2"}]`))
			return
		}
		// DELETE close
		w.WriteHeader(http.StatusOK)
	})
	defer done()

	res, err := c.Execute(context.Background(), &Command{Action: ActionCloseBuy, Symbol: "EURUSD"})
	if err != nil {
		t.Fatalf("execute close: %v", err)
	}
	if res.BrokerOrderID != "closed:1" {
		t.Fatalf("expected closed:1 (only EURUSD), got %q", res.BrokerOrderID)
	}
}

func TestExecute_CancelOrders(t *testing.T) {
	c, done := newAuthedClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet {
			_, _ = w.Write([]byte(`[{"symbol":"EURUSD","orderId":"O1"},{"symbol":"USDJPY","orderId":"O2"}]`))
			return
		}
		w.WriteHeader(http.StatusNoContent)
	})
	defer done()

	res, err := c.Execute(context.Background(), &Command{Action: ActionCancel, Symbol: "EURUSD"})
	if err != nil {
		t.Fatalf("execute cancel: %v", err)
	}
	if res.BrokerOrderID != "cancelled:1" {
		t.Fatalf("expected cancelled:1, got %q", res.BrokerOrderID)
	}
}

func TestExecute_PlaceOrderBrokerError(t *testing.T) {
	c, done := newAuthedClient(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte(`{"error":"bad order"}`))
	})
	defer done()

	if _, err := c.Execute(context.Background(), &Command{Action: ActionBuy, Symbol: "EURUSD", Volume: 0.1}); err == nil {
		t.Fatal("expected error on broker 400")
	}
}
