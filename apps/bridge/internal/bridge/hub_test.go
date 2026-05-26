package bridge

import (
	"sync"
	"testing"
)

type mockWSConn struct {
	mu       sync.Mutex
	messages []any
	closed   bool
}

func (m *mockWSConn) WriteJSON(v any) error {
	m.mu.Lock()
	m.messages = append(m.messages, v)
	m.mu.Unlock()
	return nil
}

func (m *mockWSConn) Close() error {
	m.mu.Lock()
	m.closed = true
	m.mu.Unlock()
	return nil
}

func TestHubRegisterAndGet(t *testing.T) {
	hub := NewHub()
	ws := &mockWSConn{}
	conn := NewConn(ws, "inst-1")
	hub.Register(conn)

	got, ok := hub.Get("inst-1")
	if !ok || got != conn {
		t.Fatal("expected registered conn")
	}
}

func TestHubEvictsOldConnection(t *testing.T) {
	hub := NewHub()
	old := &mockWSConn{}
	hub.Register(NewConn(old, "inst-1"))

	hub.Register(NewConn(&mockWSConn{}, "inst-1"))

	old.mu.Lock()
	closed := old.closed
	old.mu.Unlock()
	if !closed {
		t.Fatal("expected old connection to be closed on eviction")
	}
}

func TestHubUnregisterOnlyOwner(t *testing.T) {
	hub := NewHub()
	first := NewConn(&mockWSConn{}, "inst-1")
	hub.Register(first)

	second := NewConn(&mockWSConn{}, "inst-1")
	hub.Register(second)

	// Unregistering the old conn must not remove the new one.
	hub.Unregister(first)
	if _, ok := hub.Get("inst-1"); !ok {
		t.Fatal("expected second conn to remain in hub")
	}
}

func TestHubUnregister(t *testing.T) {
	hub := NewHub()
	conn := NewConn(&mockWSConn{}, "inst-1")
	hub.Register(conn)

	hub.Unregister(conn)
	if _, ok := hub.Get("inst-1"); ok {
		t.Fatal("expected conn to be removed from hub")
	}
}

func TestHubGetMissing(t *testing.T) {
	hub := NewHub()
	if _, ok := hub.Get("no-such-instance"); ok {
		t.Fatal("expected miss for unregistered instanceID")
	}
}
