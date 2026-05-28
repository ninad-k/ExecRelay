package bridge

import (
	"encoding/json"
	"log/slog"
	"net/http"
	"time"

	"github.com/gorilla/websocket"
	"github.com/nats-io/nats.go"
	"github.com/ninadk/execrelay/internal/obs"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

var upgrader = websocket.Upgrader{
	HandshakeTimeout: 5 * time.Second,
	CheckOrigin:      func(r *http.Request) bool { return true },
}

const (
	registerReadDeadline = 10 * time.Second
	pingInterval         = 30 * time.Second
	pongDeadline         = 50 * time.Second
)

// FillPublisher publishes fill JSON and event JSON to NATS.
type FillPublisher interface {
	Publish(subject string, data []byte) error
}

type Handler struct {
	hub       *Hub
	publisher FillPublisher
	nc        *nats.Conn
	authToken string
}

func NewHandler(hub *Hub) *Handler {
	return &Handler{hub: hub}
}

func NewHandlerWithPublisher(hub *Hub, p FillPublisher) *Handler {
	return &Handler{hub: hub, publisher: p}
}

func NewHandlerWithAuth(hub *Hub, nc *nats.Conn, authToken string) *Handler {
	return &Handler{hub: hub, publisher: nc, nc: nc, authToken: authToken}
}

func (h *Handler) Routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/health", h.health)
	mux.HandleFunc("/healthz", h.health)
	mux.HandleFunc("/readyz", h.readyz)
	mux.HandleFunc("/ea/ws", h.eaWS)
	mux.Handle("/metrics", promhttp.Handler())
	return obs.Middleware("bridge")(mux)
}

func (h *Handler) health(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	if h.nc != nil && !h.nc.IsConnected() {
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = w.Write([]byte(`{"service":"bridge","status":"degraded","reason":"nats_disconnected"}`))
		return
	}
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte(`{"service":"bridge","status":"ok"}`))
}

// readyz is the stricter probe used by load balancers / k8s readiness gates.
// Returns 503 if NATS isn't currently up so the LB can pull this pod.
func (h *Handler) readyz(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	natsOK := h.nc == nil || h.nc.IsConnected()
	body := map[string]any{
		"service": "bridge",
		"ok":      natsOK,
		"checks": map[string]any{
			"nats": map[string]any{"ok": natsOK},
		},
	}
	if !natsOK {
		w.WriteHeader(http.StatusServiceUnavailable)
	}
	_ = json.NewEncoder(w).Encode(body)
}

func (h *Handler) eaWS(w http.ResponseWriter, r *http.Request) {
	ws, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		slog.Error("ws upgrade", "err", err)
		return
	}

	ws.SetReadDeadline(time.Now().Add(registerReadDeadline))
	_, raw, err := ws.ReadMessage()
	if err != nil {
		slog.Error("read register", "err", err)
		ws.Close()
		return
	}
	ws.SetReadDeadline(time.Time{})

	var reg RegisterMsg
	if err := json.Unmarshal(raw, &reg); err != nil || reg.Type != TypeRegister || reg.InstanceID == "" {
		slog.Warn("invalid register message")
		ws.Close()
		return
	}

	if h.authToken != "" && !constantEqual(reg.Token, h.authToken) {
		slog.Warn("EA auth failed", "instance_id", reg.InstanceID)
		ws.Close()
		return
	}
	if h.authToken == "" {
		slog.Warn("BRIDGE_AUTH_TOKEN not set — EA auth is disabled")
	}

	conn := NewConn(ws, reg.InstanceID)
	h.hub.Register(conn)
	defer h.hub.Unregister(conn)

	if err := conn.WriteJSON(RegisteredMsg{Type: TypeRegistered}); err != nil {
		slog.Error("ack register", "instance_id", reg.InstanceID, "err", err)
		return
	}

	slog.Info("EA registered", "instance_id", reg.InstanceID, "platform", reg.Platform,
		"broker", reg.Broker, "account", reg.AccountNumber)

	h.publishEvent("events.ea.connected", map[string]string{
		"instance_id":    reg.InstanceID,
		"account_number": reg.AccountNumber,
		"broker":         reg.Broker,
		"platform":       reg.Platform,
		"ea_version":     reg.EAVersion,
	})

	done := make(chan struct{})
	defer close(done)

	go func() {
		ticker := time.NewTicker(pingInterval)
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				if err := conn.WriteJSON(PingMsg{Type: TypePing}); err != nil {
					ws.Close()
					return
				}
			case <-done:
				return
			}
		}
	}()

	go func() {
		ticker := time.NewTicker(30 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				if conn.IsZombie(90 * time.Second) {
					slog.Warn("zombie EA detected, closing", "instance_id", reg.InstanceID)
					ws.Close()
					return
				}
			case <-done:
				return
			}
		}
	}()

	defer func() {
		h.publishEvent("events.ea.disconnected", map[string]string{
			"instance_id": reg.InstanceID,
		})
		slog.Info("EA disconnected", "instance_id", reg.InstanceID)
	}()

	for {
		ws.SetReadDeadline(time.Now().Add(pongDeadline))
		_, raw, err := ws.ReadMessage()
		if err != nil {
			if !websocket.IsCloseError(err, websocket.CloseNormalClosure, websocket.CloseGoingAway) {
				slog.Error("read from EA", "instance_id", reg.InstanceID, "err", err)
			}
			return
		}
		h.handleEAMessage(conn, raw)
	}
}

func (h *Handler) handleEAMessage(conn *Conn, raw []byte) {
	var typed TypedMsg
	if err := json.Unmarshal(raw, &typed); err != nil {
		return
	}
	switch typed.Type {
	case TypeFill:
		var fill FillMsg
		if err := json.Unmarshal(raw, &fill); err != nil {
			slog.Error("decode fill", "instance_id", conn.instanceID, "err", err)
			return
		}
		fillsReceived.Inc()
		slog.Info("fill received", "trace_id", fill.TraceID, "status", fill.Status,
			"broker_order", fill.BrokerOrderID, "error", fill.ErrorMessage)
		if h.publisher != nil {
			subject := "fills." + conn.instanceID + "." + fill.TraceID
			if err := h.publisher.Publish(subject, raw); err != nil {
				slog.Error("publish fill", "err", err)
			}
		}
	case TypePong:
		// heartbeat ack
	case TypeHeartbeat:
		conn.UpdateHeartbeat()
	default:
		slog.Warn("unknown message type from EA", "type", typed.Type, "instance_id", conn.instanceID)
	}
}

func (h *Handler) publishEvent(subject string, fields map[string]string) {
	if h.publisher == nil {
		return
	}
	data, err := json.Marshal(fields)
	if err != nil {
		return
	}
	if err := h.publisher.Publish(subject, data); err != nil {
		slog.Warn("publish event", "subject", subject, "err", err)
	}
}

// constantEqual is a timing-safe string comparison.
func constantEqual(a, b string) bool {
	if len(a) != len(b) {
		return false
	}
	var diff byte
	for i := 0; i < len(a); i++ {
		diff |= a[i] ^ b[i]
	}
	return diff == 0
}
