// Package obs provides shared observability primitives for ExecRelay Go
// services. Every HTTP request gets a request_id (UUID-like hex), structured
// JSON access log on entry and exit, and propagation of trace_id headers so an
// operator can pivot from a failed trade back to the originating webhook line.
package obs

import (
	"bufio"
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"strconv"
	"strings"
	"time"
)

type ctxKey int

const (
	ctxRequestID ctxKey = iota
	ctxTraceID
)

const (
	HeaderRequestID = "X-Request-ID"
	HeaderTraceID   = "X-ExecRelay-Trace-ID"
)

// NewID returns a 16-byte hex identifier safe for request_id / trace_id use.
// Falls back to a timestamp-derived value on crypto/rand failure so the
// process never dies for an ID.
func NewID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return strconv.FormatInt(time.Now().UnixNano(), 16)
	}
	return hex.EncodeToString(b[:])
}

// RequestIDFromContext returns the request_id installed by the middleware,
// or "" if none. Handlers use this to include the ID in error responses and
// downstream-call headers.
func RequestIDFromContext(ctx context.Context) string {
	if v, ok := ctx.Value(ctxRequestID).(string); ok {
		return v
	}
	return ""
}

// TraceIDFromContext returns the trace_id installed by the middleware.
func TraceIDFromContext(ctx context.Context) string {
	if v, ok := ctx.Value(ctxTraceID).(string); ok {
		return v
	}
	return ""
}

type statusRecorder struct {
	http.ResponseWriter
	status   int
	bytes    int
	hijacked bool
}

func (r *statusRecorder) WriteHeader(code int) {
	r.status = code
	r.ResponseWriter.WriteHeader(code)
}

func (r *statusRecorder) Write(b []byte) (int, error) {
	n, err := r.ResponseWriter.Write(b)
	r.bytes += n
	return n, err
}

// Hijack delegates to the underlying ResponseWriter so the middleware stays
// transparent to websocket upgrades (gorilla/websocket needs Hijacker). After
// hijacking we mark the connection so the access-log line uses status 101.
func (r *statusRecorder) Hijack() (net.Conn, *bufio.ReadWriter, error) {
	hj, ok := r.ResponseWriter.(http.Hijacker)
	if !ok {
		return nil, nil, errors.New("response writer does not support hijacking")
	}
	conn, brw, err := hj.Hijack()
	if err == nil {
		r.hijacked = true
		if r.status == http.StatusOK {
			r.status = http.StatusSwitchingProtocols
		}
	}
	return conn, brw, err
}

// Flush keeps streaming responses (SSE, etc.) working when the middleware
// shadows the underlying writer.
func (r *statusRecorder) Flush() {
	if f, ok := r.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

// Middleware returns an http.Handler that:
//   - assigns or honors X-Request-ID
//   - propagates X-ExecRelay-Trace-ID (also accepts W3C traceparent's trace-id)
//   - emits one structured log line per request with status + latency
//   - sets both headers on the response so the caller can correlate
//
// `service` is included in every log line so a multi-service log stream
// remains pivotable.
func Middleware(service string) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			rid := strings.TrimSpace(r.Header.Get(HeaderRequestID))
			if rid == "" {
				rid = NewID()
			}
			tid := strings.TrimSpace(r.Header.Get(HeaderTraceID))
			if tid == "" {
				tid = traceFromW3C(r.Header.Get("Traceparent"))
			}

			ctx := context.WithValue(r.Context(), ctxRequestID, rid)
			if tid != "" {
				ctx = context.WithValue(ctx, ctxTraceID, tid)
			}
			r = r.WithContext(ctx)
			w.Header().Set(HeaderRequestID, rid)
			if tid != "" {
				w.Header().Set(HeaderTraceID, tid)
			}

			rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
			start := time.Now()
			next.ServeHTTP(rec, r)
			latencyMS := float64(time.Since(start).Microseconds()) / 1000.0

			attrs := []any{
				"event", "request",
				"service", service,
				"request_id", rid,
				"method", r.Method,
				"path", r.URL.Path,
				"status", rec.status,
				"latency_ms", latencyMS,
				"bytes", rec.bytes,
				"client", clientIP(r),
				"ua", truncate(r.UserAgent(), 120),
			}
			if tid != "" {
				attrs = append(attrs, "trace_id", tid)
			}
			level := slog.LevelInfo
			switch {
			case rec.status >= 500:
				level = slog.LevelError
			case rec.status >= 400:
				level = slog.LevelWarn
			}
			slog.LogAttrs(r.Context(), level, "http_request", slogAttrs(attrs)...)
		})
	}
}

// traceFromW3C extracts the 32-char trace-id from a W3C traceparent header.
// Returns "" if the header is malformed.
func traceFromW3C(traceparent string) string {
	// Format: 00-<trace-id 32 hex>-<span-id 16 hex>-<flags 2 hex>
	if len(traceparent) >= 55 && traceparent[2] == '-' && traceparent[35] == '-' {
		return traceparent[3:35]
	}
	return ""
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

func truncate(s string, max int) string {
	if len(s) <= max {
		return s
	}
	return s[:max]
}

func slogAttrs(kv []any) []slog.Attr {
	out := make([]slog.Attr, 0, len(kv)/2)
	for i := 0; i+1 < len(kv); i += 2 {
		key, _ := kv[i].(string)
		out = append(out, slog.Any(key, kv[i+1]))
	}
	return out
}
