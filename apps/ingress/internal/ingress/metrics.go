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

	licenseConfigWarnings = promauto.NewGaugeVec(prometheus.GaugeOpts{
		Name: "ingress_license_config_warnings",
		Help: "Per-license configuration warnings (1 = warning active, 0 = cleared).",
	}, []string{"license_id", "issue"})

	tradingHaltedGauge = promauto.NewGauge(prometheus.GaugeOpts{
		Name: "ingress_trading_halted",
		Help: "Kill-switch state. 1 = all webhooks are rejected with trading_halted; 0 = normal operation.",
	})

	// ML webhook (ADR 0008) metrics.
	mlWebhookRequests = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "ingress_ml_webhook_requests_total",
		Help: "Total /webhook/ml requests by outcome: accepted, skipped (NOTHING in enforce mode), fail_open (predictor down/erroring), rejected (auth/parse failure).",
	}, []string{"outcome"})

	mlWebhookDuration = promauto.NewHistogram(prometheus.HistogramOpts{
		Name:    "ingress_ml_webhook_duration_seconds",
		Help:    "/webhook/ml request latency, including the synchronous ml-predictor call.",
		Buckets: prometheus.DefBuckets,
	})

	mlPredictorErrors = promauto.NewCounter(prometheus.CounterOpts{
		Name: "ingress_ml_predictor_errors_total",
		Help: "Total errors calling ml-predictor's /predict endpoint (timeout, connection refused, non-2xx, malformed response). Each of these fails open.",
	})
)

func reportTradingHalted(halted bool) {
	if halted {
		tradingHaltedGauge.Set(1)
	} else {
		tradingHaltedGauge.Set(0)
	}
}

// ReportLicenseWarnings sets the gauge for each current warning and clears
// any gauges from a previous audit that are no longer active. Call at startup
// and after each license hot-reload.
func ReportLicenseWarnings(current []LicenseWarning) {
	licenseConfigWarnings.Reset()
	for _, w := range current {
		licenseConfigWarnings.WithLabelValues(w.LicenseID, w.Issue).Set(1)
	}
}

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
		switch r.URL.Path {
		case "/webhook":
			rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
			start := time.Now()
			next.ServeHTTP(rec, r)
			webhookDuration.Observe(time.Since(start).Seconds())
			webhookRequests.WithLabelValues(strconv.Itoa(rec.status)).Inc()
		case "/webhook/ml":
			rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
			start := time.Now()
			next.ServeHTTP(rec, r)
			mlWebhookDuration.Observe(time.Since(start).Seconds())
		default:
			next.ServeHTTP(w, r)
		}
	})
}

func recordRejection(reason string) {
	webhookRejections.WithLabelValues(reason).Inc()
}

// recordMLOutcome increments the /webhook/ml outcome counter. Outcome is one
// of: accepted, skipped, fail_open, rejected.
func recordMLOutcome(outcome string) {
	mlWebhookRequests.WithLabelValues(outcome).Inc()
}
