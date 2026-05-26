package ingress

import (
	"net/http"
	"strconv"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
)

var (
	webhookRequests = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "ingress_webhook_requests_total",
		Help: "Total webhook requests by HTTP status code.",
	}, []string{"status"})

	webhookDuration = promauto.NewHistogram(prometheus.HistogramOpts{
		Name:    "ingress_webhook_duration_seconds",
		Help:    "Webhook request latency.",
		Buckets: prometheus.DefBuckets,
	})

	webhookRejections = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "ingress_rejections_total",
		Help: "Total webhook rejections by reason code.",
	}, []string{"reason"})
)

type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (r *statusRecorder) WriteHeader(code int) {
	r.status = code
	r.ResponseWriter.WriteHeader(code)
}

func metricsMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/webhook" {
			next.ServeHTTP(w, r)
			return
		}
		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		start := time.Now()
		next.ServeHTTP(rec, r)
		webhookDuration.Observe(time.Since(start).Seconds())
		webhookRequests.WithLabelValues(strconv.Itoa(rec.status)).Inc()
	})
}

func recordRejection(reason string) {
	webhookRejections.WithLabelValues(reason).Inc()
}
