package bridge

import (
	"log/slog"

	oldproto "github.com/golang/protobuf/proto"
	"github.com/nats-io/nats.go"
	execrelaypb "github.com/ninadk/execrelay/packages/proto"
)

// maxDeliverBeforeTerm is the number of redeliveries before an unroutable
// message is terminated so it is never redelivered again.
const maxDeliverBeforeTerm = 10

// Subscriber receives Signal messages from JetStream and dispatches them to the hub.
// It creates separate durable consumers for mt5 and mt4 subjects.
type Subscriber struct {
	js       nats.JetStreamContext
	hub      *Hub
	stream   string
	consumer string
}

func NewSubscriber(js nats.JetStreamContext, hub *Hub, stream, consumer string) *Subscriber {
	return &Subscriber{js: js, hub: hub, stream: stream, consumer: consumer}
}

// Subscribe creates two durable push consumers — one for mt5, one for mt4 signals.
func (s *Subscriber) Subscribe() ([]*nats.Subscription, error) {
	mt5Sub, err := s.js.Subscribe(
		SignalSubjectMT5,
		s.dispatch,
		nats.Durable(s.consumer+"-mt5"),
		nats.BindStream(s.stream),
		nats.AckExplicit(),
		nats.DeliverNew(),
	)
	if err != nil {
		return nil, err
	}

	mt4Sub, err := s.js.Subscribe(
		SignalSubjectMT4,
		s.dispatch,
		nats.Durable(s.consumer+"-mt4"),
		nats.BindStream(s.stream),
		nats.AckExplicit(),
		nats.DeliverNew(),
	)
	if err != nil {
		_ = mt5Sub.Drain()
		return nil, err
	}

	return []*nats.Subscription{mt5Sub, mt4Sub}, nil
}

func (s *Subscriber) dispatch(msg *nats.Msg) {
	meta, err := msg.Metadata()
	if err == nil && meta.NumDelivered > maxDeliverBeforeTerm {
		slog.Warn("terminating unroutable signal", "deliveries", meta.NumDelivered)
		_ = msg.Term()
		return
	}

	var signal execrelaypb.Signal
	if err := oldproto.Unmarshal(msg.Data, &signal); err != nil {
		slog.Error("unmarshal signal", "err", err)
		_ = msg.Term()
		return
	}

	conn, ok := s.hub.Get(signal.InstanceId)
	if !ok {
		signalsNacked.Inc()
		_ = msg.Nak()
		return
	}

	params := make(map[string]string, len(signal.Params))
	for _, p := range signal.Params {
		params[p.Key] = p.Value
	}

	if err := conn.WriteJSON(SignalMsg{
		Type:    TypeSignal,
		TraceID: signal.TraceId,
		Command: signal.Command,
		Symbol:  signal.Symbol,
		Params:  params,
	}); err != nil {
		slog.Error("send signal to EA", "instance_id", signal.InstanceId, "err", err)
		_ = msg.Nak()
		return
	}

	signalsDispatched.Inc()
	_ = msg.Ack()
}
