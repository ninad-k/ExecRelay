package dxtrade

import (
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
)

var (
	commandsProcessed = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "dxtrade_commands_processed_total",
		Help: "Total commands processed by DXTrade adapter.",
	}, []string{"command"})

	executionLatency = promauto.NewHistogram(prometheus.HistogramOpts{
		Name:    "dxtrade_execution_latency_seconds",
		Help:    "Latency of DXTrade command execution.",
		Buckets: prometheus.DefBuckets,
	})

	circuitBreakerTrips = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "dxtrade_circuit_breaker_trips_total",
		Help: "Number of times circuit breaker tripped per broker.",
	}, []string{"broker"})

	brokerFailures = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "dxtrade_broker_failures_total",
		Help: "Total broker execution failures.",
	}, []string{"broker", "error_type"})
)

func RecordCommandProcessed(command string) {
	commandsProcessed.WithLabelValues(command).Inc()
}

func RecordExecutionLatency(seconds float64) {
	executionLatency.Observe(seconds)
}

func RecordCircuitBreakerTrip(broker string) {
	circuitBreakerTrips.WithLabelValues(broker).Inc()
}

func RecordBrokerFailure(broker, errorType string) {
	brokerFailures.WithLabelValues(broker, errorType).Inc()
}
