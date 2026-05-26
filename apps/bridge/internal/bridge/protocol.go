package bridge

// Message types used on the bridge<->EA WebSocket.
const (
	TypeRegister   = "register"
	TypeRegistered = "registered"
	TypeSignal     = "signal"
	TypeFill       = "fill"
	TypePing       = "ping"
	TypePong       = "pong"
	TypeHeartbeat  = "heartbeat"
)

// TypedMsg is used to peek at the type field before full decode.
type TypedMsg struct {
	Type string `json:"type"`
}

// RegisterMsg is sent by the EA immediately after connecting.
type RegisterMsg struct {
	Type          string `json:"type"`
	InstanceID    string `json:"instance_id"`
	Token         string `json:"token"`
	AccountNumber string `json:"account_number"`
	Broker        string `json:"broker"`
	Platform      string `json:"platform"`
	EAVersion     string `json:"ea_version"`
}

// RegisteredMsg is sent by the bridge to acknowledge a successful registration.
type RegisteredMsg struct {
	Type string `json:"type"`
}

// SignalMsg is sent by the bridge to deliver a trading command to the EA.
type SignalMsg struct {
	Type    string            `json:"type"`
	TraceID string            `json:"trace_id"`
	Command string            `json:"command"`
	Symbol  string            `json:"symbol"`
	Params  map[string]string `json:"params,omitempty"`
}

// FillMsg is sent by the EA to report order execution result.
type FillMsg struct {
	Type          string `json:"type"`
	TraceID       string `json:"trace_id"`
	Status        string `json:"status"` // "filled", "rejected", "error"
	BrokerOrderID string `json:"broker_order_id,omitempty"`
	ErrorCode     string `json:"error_code,omitempty"`
	ErrorMessage  string `json:"error_message,omitempty"`
}

// PingMsg / PongMsg are application-level heartbeat messages.
type PingMsg struct{ Type string `json:"type"` }
type PongMsg struct{ Type string `json:"type"` }

// HeartbeatMsg is sent by the EA to report account health.
type HeartbeatMsg struct {
	Type       string  `json:"type"`
	FreeMargin float64 `json:"free_margin"`
	Equity     float64 `json:"equity"`
	UptimeSecs int64   `json:"uptime_secs"`
}
