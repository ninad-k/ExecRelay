package dxtrade

import (
	"context"
	"encoding/json"
	"log"
	"time"

	oldproto "github.com/golang/protobuf/proto"
	"github.com/nats-io/nats.go"
	execrelaypb "github.com/ninadk/execrelay/packages/proto"
)

const (
	signalSubject      = "signals.dxtrade.>"
	maxDeliverBeforeTerm = 10
)

// FillPublisher sends fill notifications back onto NATS.
type FillPublisher interface {
	Publish(subject string, data []byte) error
}

// Subscriber consumes DXTrade signals from JetStream and executes them.
type Subscriber struct {
	js       nats.JetStreamContext
	clients  map[string]*Client // keyed by instanceID
	fills    FillPublisher
	stream   string
	consumer string
}

func NewSubscriber(js nats.JetStreamContext, clients map[string]*Client, fills FillPublisher, stream, consumer string) *Subscriber {
	return &Subscriber{
		js:       js,
		clients:  clients,
		fills:    fills,
		stream:   stream,
		consumer: consumer,
	}
}

func (s *Subscriber) Subscribe() (*nats.Subscription, error) {
	return s.js.Subscribe(
		signalSubject,
		s.dispatch,
		nats.Durable(s.consumer),
		nats.BindStream(s.stream),
		nats.AckExplicit(),
		nats.DeliverNew(),
	)
}

func (s *Subscriber) dispatch(msg *nats.Msg) {
	meta, err := msg.Metadata()
	if err == nil && meta.NumDelivered > maxDeliverBeforeTerm {
		log.Printf("dxtrade: terminating unroutable signal after %d deliveries", meta.NumDelivered)
		_ = msg.Term()
		return
	}

	var signal execrelaypb.Signal
	if err := oldproto.Unmarshal(msg.Data, &signal); err != nil {
		log.Printf("dxtrade: unmarshal signal: %v", err)
		_ = msg.Term()
		return
	}

	client, ok := s.clients[signal.InstanceId]
	if !ok {
		log.Printf("dxtrade: no client for instance %q, nacking", signal.InstanceId)
		_ = msg.Nak()
		return
	}

	params := make(map[string]string, len(signal.Params))
	for _, p := range signal.Params {
		params[p.Key] = p.Value
	}

	cmd, err := ParseCommand(signal.TraceId, signal.Command, signal.Symbol, params)
	if err != nil {
		log.Printf("dxtrade: parse command: %v", err)
		_ = msg.Term()
		return
	}

	RecordCommandProcessed(cmd.Action)
	start := time.Now()
	result, err := client.Execute(context.Background(), cmd)
	RecordExecutionLatency(time.Since(start).Seconds())

	if err != nil {
		log.Printf("dxtrade: execute %s for %s: %v", cmd.Action, signal.InstanceId, err)
		_ = s.publishFill(signal.InstanceId, signal.TraceId, &Result{
			Status:       StatusRejected,
			ErrorMessage: err.Error(),
		})
		_ = msg.Nak()
		return
	}

	_ = s.publishFill(signal.InstanceId, signal.TraceId, result)
	_ = msg.Ack()
}

type fillPayload struct {
	Type          string `json:"type"`
	TraceID       string `json:"trace_id"`
	Status        string `json:"status"`
	BrokerOrderID string `json:"broker_order_id,omitempty"`
	ErrorCode     string `json:"error_code,omitempty"`
	ErrorMessage  string `json:"error_message,omitempty"`
}

func (s *Subscriber) publishFill(instanceID, traceID string, result *Result) error {
	payload := fillPayload{
		Type:          "fill",
		TraceID:       traceID,
		Status:        result.Status,
		BrokerOrderID: result.BrokerOrderID,
		ErrorCode:     result.ErrorCode,
		ErrorMessage:  result.ErrorMessage,
	}
	data, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	subject := "fills." + instanceID + "." + traceID
	return s.fills.Publish(subject, data)
}
