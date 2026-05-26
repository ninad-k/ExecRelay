package bridge

import (
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

func TestEAWebSocketFlow(t *testing.T) {
	hub := NewHub()
	server := httptest.NewServer(NewHandler(hub).Routes())
	defer server.Close()

	wsURL := "ws" + strings.TrimPrefix(server.URL, "http") + "/ea/ws"
	ws, _, err := websocket.DefaultDialer.Dial(wsURL, nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer ws.Close()

	reg := RegisterMsg{
		Type:          TypeRegister,
		InstanceID:    "test-inst-1",
		Platform:      "mt5",
		Broker:        "testbroker",
		AccountNumber: "12345",
		EAVersion:     "1.0",
	}
	if err := ws.WriteJSON(reg); err != nil {
		t.Fatalf("write register: %v", err)
	}

	var ack TypedMsg
	if err := ws.ReadJSON(&ack); err != nil {
		t.Fatalf("read ack: %v", err)
	}
	if ack.Type != TypeRegistered {
		t.Fatalf("expected %q ack, got %q", TypeRegistered, ack.Type)
	}

	conn, ok := hub.Get("test-inst-1")
	if !ok {
		t.Fatal("expected conn registered in hub")
	}

	sig := SignalMsg{
		Type:    TypeSignal,
		TraceID: "trace-abc",
		Command: "buy",
		Symbol:  "EURUSD",
		Params:  map[string]string{"vol_lots": "0.1", "sl_pips": "20"},
	}
	if err := conn.WriteJSON(sig); err != nil {
		t.Fatalf("write signal: %v", err)
	}

	var received SignalMsg
	if err := ws.ReadJSON(&received); err != nil {
		t.Fatalf("read signal on EA side: %v", err)
	}
	if received.TraceID != "trace-abc" || received.Command != "buy" || received.Symbol != "EURUSD" {
		t.Fatalf("unexpected signal: %+v", received)
	}

	fill := FillMsg{
		Type:          TypeFill,
		TraceID:       "trace-abc",
		Status:        "filled",
		BrokerOrderID: "99999",
	}
	if err := ws.WriteJSON(fill); err != nil {
		t.Fatalf("write fill: %v", err)
	}
}

func TestEAWebSocketRejectsNoRegister(t *testing.T) {
	server := httptest.NewServer(NewHandler(NewHub()).Routes())
	defer server.Close()

	wsURL := "ws" + strings.TrimPrefix(server.URL, "http") + "/ea/ws"
	ws, _, err := websocket.DefaultDialer.Dial(wsURL, nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer ws.Close()

	// Send a non-register message first — server should close the connection.
	if err := ws.WriteJSON(PingMsg{Type: TypePing}); err != nil {
		t.Fatalf("write: %v", err)
	}

	ws.SetReadDeadline(time.Now().Add(2 * time.Second))
	_, _, err = ws.ReadMessage()
	if err == nil {
		t.Fatal("expected server to close connection after invalid register")
	}
}

func TestEAWebSocketUnregistersOnDisconnect(t *testing.T) {
	hub := NewHub()
	server := httptest.NewServer(NewHandler(hub).Routes())
	defer server.Close()

	wsURL := "ws" + strings.TrimPrefix(server.URL, "http") + "/ea/ws"
	ws, _, err := websocket.DefaultDialer.Dial(wsURL, nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}

	if err := ws.WriteJSON(RegisterMsg{Type: TypeRegister, InstanceID: "inst-disc"}); err != nil {
		t.Fatalf("write register: %v", err)
	}
	var ack TypedMsg
	_ = ws.ReadJSON(&ack)

	ws.Close()

	// Give the server goroutine time to process the disconnect.
	deadline := time.Now().Add(500 * time.Millisecond)
	for time.Now().Before(deadline) {
		if _, ok := hub.Get("inst-disc"); !ok {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("expected conn to be removed from hub after disconnect")
}
