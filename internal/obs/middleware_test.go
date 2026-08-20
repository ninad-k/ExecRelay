package obs

import (
	"bytes"
	"context"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// captureSlog swaps slog's default logger for one writing JSON to the
// returned buffer, restoring the previous logger via t.Cleanup.
func captureSlog(t *testing.T) *bytes.Buffer {
	t.Helper()
	var buf bytes.Buffer
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewJSONHandler(&buf, nil)))
	t.Cleanup(func() { slog.SetDefault(prev) })
	return &buf
}

func TestMiddlewareAssignsRequestID(t *testing.T) {
	var gotRID string
	h := Middleware("test")(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotRID = RequestIDFromContext(r.Context())
		w.WriteHeader(http.StatusOK)
	}))

	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, httptest.NewRequest(http.MethodGet, "/", nil))

	if gotRID == "" {
		t.Fatal("expected request_id in context")
	}
	if rr.Header().Get(HeaderRequestID) != gotRID {
		t.Fatalf("response header request_id mismatch: %q vs %q",
			rr.Header().Get(HeaderRequestID), gotRID)
	}
	if len(gotRID) != 32 {
		t.Fatalf("expected 32-char hex id, got %d", len(gotRID))
	}
}

func TestMiddlewareHonorsIncomingRequestID(t *testing.T) {
	want := "my-supplied-id"
	var gotRID string
	h := Middleware("test")(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotRID = RequestIDFromContext(r.Context())
		w.WriteHeader(http.StatusOK)
	}))

	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set(HeaderRequestID, want)
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, req)

	if gotRID != want {
		t.Fatalf("expected %q, got %q", want, gotRID)
	}
	if rr.Header().Get(HeaderRequestID) != want {
		t.Fatalf("response header not echoed: %q", rr.Header().Get(HeaderRequestID))
	}
}

func TestMiddlewarePropagatesTraceID(t *testing.T) {
	tid := strings.Repeat("a", 32)
	var seenTID string
	h := Middleware("test")(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seenTID = TraceIDFromContext(r.Context())
		w.WriteHeader(http.StatusOK)
	}))

	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set(HeaderTraceID, tid)
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, req)

	if seenTID != tid {
		t.Fatalf("expected trace_id %q, got %q", tid, seenTID)
	}
	if rr.Header().Get(HeaderTraceID) != tid {
		t.Fatalf("response trace_id missing: %q", rr.Header().Get(HeaderTraceID))
	}
}

func TestMiddlewareExtractsW3CTraceparent(t *testing.T) {
	// 00-<32 hex trace>-<16 hex span>-01
	trace := strings.Repeat("b", 32)
	span := strings.Repeat("c", 16)
	tp := "00-" + trace + "-" + span + "-01"
	var seen string
	h := Middleware("test")(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = TraceIDFromContext(r.Context())
	}))

	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set("Traceparent", tp)
	h.ServeHTTP(httptest.NewRecorder(), req)

	if seen != trace {
		t.Fatalf("expected trace %q extracted, got %q", trace, seen)
	}
}

func TestMiddlewareSuppressesSuccessfulHealthChecks(t *testing.T) {
	logs := captureSlog(t)
	h := Middleware("test")(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	for _, p := range []string{"/health", "/healthz", "/readyz", "/metrics"} {
		h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, p, nil))
	}
	if logs.Len() != 0 {
		t.Fatalf("expected zero log lines for successful poll paths, got: %s", logs.String())
	}
}

func TestMiddlewareStillLogsFailingHealthChecks(t *testing.T) {
	logs := captureSlog(t)
	h := Middleware("test")(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/health", nil))
	if !strings.Contains(logs.String(), `"path":"/health"`) {
		t.Fatalf("expected a failing health check to still be logged, got: %s", logs.String())
	}
}

func TestMiddlewareAlwaysQuietPathNeverLogs(t *testing.T) {
	logs := captureSlog(t)
	h := Middleware("test", "/webhook")(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized) // even a rejection must stay silent here
	}))
	h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodPost, "/webhook", nil))
	if logs.Len() != 0 {
		t.Fatalf("expected an always-quiet path to never log regardless of status, got: %s", logs.String())
	}
}

func TestMiddlewareStillLogsOrdinaryPaths(t *testing.T) {
	logs := captureSlog(t)
	h := Middleware("test", "/webhook")(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/some-other-path", nil))
	if !strings.Contains(logs.String(), `"path":"/some-other-path"`) {
		t.Fatalf("expected an ordinary path to still be logged, got: %s", logs.String())
	}
}

func TestContextHelpersTolerateMissing(t *testing.T) {
	if RequestIDFromContext(context.Background()) != "" {
		t.Fatal("expected empty request_id on bare ctx")
	}
	if TraceIDFromContext(context.Background()) != "" {
		t.Fatal("expected empty trace_id on bare ctx")
	}
}
