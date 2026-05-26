package bridge

import (
	"sync"
	"time"
)

// WSConn is the subset of websocket.Conn used by the hub, enabling test mocking.
type WSConn interface {
	WriteJSON(v any) error
	Close() error
}

// Conn wraps a WebSocket connection with write serialization and registration info.
type Conn struct {
	ws            WSConn
	instanceID    string
	mu            sync.Mutex
	lastHeartbeat time.Time
}

func NewConn(ws WSConn, instanceID string) *Conn {
	return &Conn{ws: ws, instanceID: instanceID}
}

func (c *Conn) WriteJSON(v any) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.ws.WriteJSON(v)
}

func (c *Conn) Close() error {
	return c.ws.Close()
}

func (c *Conn) UpdateHeartbeat() {
	c.mu.Lock()
	c.lastHeartbeat = time.Now()
	c.mu.Unlock()
}

func (c *Conn) IsZombie(threshold time.Duration) bool {
	c.mu.Lock()
	lh := c.lastHeartbeat
	c.mu.Unlock()
	if lh.IsZero() {
		return false // no heartbeat yet — still connecting
	}
	return time.Since(lh) > threshold
}

// Hub maps instanceID -> active Conn.
type Hub struct {
	mu    sync.RWMutex
	conns map[string]*Conn
}

func NewHub() *Hub {
	return &Hub{conns: make(map[string]*Conn)}
}

// Register adds conn to the hub. Any previous connection for the same instanceID is closed.
func (h *Hub) Register(c *Conn) {
	h.mu.Lock()
	if old, ok := h.conns[c.instanceID]; ok {
		old.Close()
	} else {
		eaConnectionsActive.Inc()
	}
	h.conns[c.instanceID] = c
	h.mu.Unlock()
}

// Unregister removes conn from the hub only if it is still the active connection.
func (h *Hub) Unregister(c *Conn) {
	h.mu.Lock()
	if h.conns[c.instanceID] == c {
		delete(h.conns, c.instanceID)
		eaConnectionsActive.Dec()
	}
	h.mu.Unlock()
}

func (h *Hub) Get(instanceID string) (*Conn, bool) {
	h.mu.RLock()
	c, ok := h.conns[instanceID]
	h.mu.RUnlock()
	return c, ok
}
