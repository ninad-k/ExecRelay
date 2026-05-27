package bridge

import (
	"errors"

	"github.com/nats-io/nats.go"
)

const (
	// signalSubjectFilter covers both mt4 and mt5 platform subjects.
	// DXTrade signals (signals.dxtrade.>) are handled by the dxtrade service.
	SignalSubjectMT5    = "signals.mt5.>"
	SignalSubjectMT4    = "signals.mt4.>"
	signalSubjectFilter = "signals.>" // stream capture pattern (all platforms)
	fillSubjectFilter   = "fills.>"
)

// EnsureStream creates a JetStream stream if it does not already exist.
func EnsureStream(js nats.JetStreamContext, streamName string) error {
	return ensureStream(js, streamName, []string{signalSubjectFilter})
}

// EnsureFillsStream creates the FILLS JetStream stream if it does not already exist.
func EnsureFillsStream(js nats.JetStreamContext) error {
	return ensureStream(js, "FILLS", []string{fillSubjectFilter})
}

// EnsureEventsStream creates the EVENTS JetStream stream if it does not already exist.
func EnsureEventsStream(js nats.JetStreamContext) error {
	return ensureStream(js, "EVENTS", []string{"events.>"})
}

func ensureStream(js nats.JetStreamContext, name string, subjects []string) error {
	_, err := js.StreamInfo(name)
	if err == nil {
		return nil
	}
	if !errors.Is(err, nats.ErrStreamNotFound) {
		return err
	}
	_, err = js.AddStream(&nats.StreamConfig{
		Name:     name,
		Subjects: subjects,
		Storage:  nats.FileStorage,
		Replicas: 1,
	})
	return err
}
