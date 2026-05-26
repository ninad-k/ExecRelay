package bridge

import (
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
)

var (
	eaConnectionsActive = promauto.NewGauge(prometheus.GaugeOpts{
		Name: "bridge_ea_connections_active",
		Help: "Number of EA WebSocket connections currently registered.",
	})

	signalsDispatched = promauto.NewCounter(prometheus.CounterOpts{
		Name: "bridge_signals_dispatched_total",
		Help: "Total signals successfully delivered to an EA.",
	})

	signalsNacked = promauto.NewCounter(prometheus.CounterOpts{
		Name: "bridge_signals_nacked_total",
		Help: "Total signals nacked because no EA was registered.",
	})

	fillsReceived = promauto.NewCounter(prometheus.CounterOpts{
		Name: "bridge_fills_received_total",
		Help: "Total fill messages received from EAs.",
	})

	consumerLagPending = promauto.NewGaugeVec(prometheus.GaugeOpts{
		Name: "bridge_consumer_lag_pending",
		Help: "Number of pending (undelivered) messages per JetStream consumer.",
	}, []string{"consumer"})
)

func SetConsumerLag(consumer string, pending int64) {
	consumerLagPending.WithLabelValues(consumer).Set(float64(pending))
}
