package ingress

import (
	"context"
	"errors"
	"time"

	"github.com/nats-io/nats.go"
)

type Publisher interface {
	Publish(ctx context.Context, subject string, payload []byte) error
	Close()
}

type NatsPublisher struct {
	conn *nats.Conn
}

func NewNatsPublisher(url string) (*NatsPublisher, error) {
	conn, err := nats.Connect(
		url,
		nats.Name("execrelay-ingress"),
		nats.Timeout(1500*time.Millisecond),
		nats.ReconnectWait(250*time.Millisecond),
		nats.MaxReconnects(-1),
	)
	if err != nil {
		return nil, err
	}
	if err := ensureEventsStream(conn); err != nil {
		conn.Close()
		return nil, err
	}
	return &NatsPublisher{conn: conn}, nil
}

// ensureEventsStream creates the EVENTS JetStream stream if it does not exist.
// Ingress publishes rejection events here; bridge publishes EA connect/disconnect.
func ensureEventsStream(conn *nats.Conn) error {
	js, err := conn.JetStream()
	if err != nil {
		return err
	}
	_, err = js.StreamInfo("EVENTS")
	if err == nil {
		return nil
	}
	if !errors.Is(err, nats.ErrStreamNotFound) {
		return err
	}
	_, err = js.AddStream(&nats.StreamConfig{
		Name:     "EVENTS",
		Subjects: []string{"events.>"},
		Storage:  nats.FileStorage,
		Replicas: 1,
	})
	return err
}

func (p *NatsPublisher) Publish(_ context.Context, subject string, payload []byte) error {
	if p == nil || p.conn == nil {
		return errors.New("nats publisher is not initialized")
	}
	return p.conn.Publish(subject, payload)
}

func (p *NatsPublisher) Close() {
	if p != nil && p.conn != nil {
		p.conn.Drain()
		p.conn.Close()
	}
}

type NoopPublisher struct{}

func (NoopPublisher) Publish(context.Context, string, []byte) error { return nil }
func (NoopPublisher) Close()                                        {}
