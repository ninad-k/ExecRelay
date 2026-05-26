package dxtrade

import (
	"fmt"
	"strconv"
)

const (
	ActionBuy       = "buy"
	ActionSell      = "sell"
	ActionBuyStop   = "buystop"
	ActionSellStop  = "sellstop"
	ActionBuyLimit  = "buylimit"
	ActionSellLimit = "selllimit"
	ActionCloseBuy  = "closebuy"
	ActionCloseSell = "closesell"
	ActionCloseAll  = "closeall"
	ActionCancel    = "cancel"

	StatusFilled   = "filled"
	StatusRejected = "rejected"
)

// Command is a parsed, executable trading instruction.
type Command struct {
	TraceID string
	Action  string
	Symbol  string
	Volume  float64 // lots
}

// Result is the outcome of executing a Command.
type Result struct {
	Status        string
	BrokerOrderID string
	ErrorCode     string
	ErrorMessage  string
}

// ParseCommand converts a signal's command string and params into a Command.
func ParseCommand(traceID, action, symbol string, params map[string]string) (*Command, error) {
	cmd := &Command{
		TraceID: traceID,
		Action:  action,
		Symbol:  symbol,
	}

	if vol, ok := params["vol_lots"]; ok {
		v, err := strconv.ParseFloat(vol, 64)
		if err != nil {
			return nil, fmt.Errorf("invalid vol_lots %q: %w", vol, err)
		}
		cmd.Volume = v
	}

	switch action {
	case ActionBuy, ActionSell,
		ActionBuyStop, ActionSellStop,
		ActionBuyLimit, ActionSellLimit:
		if cmd.Volume <= 0 {
			return nil, fmt.Errorf("vol_lots required for %s", action)
		}
	case ActionCloseBuy, ActionCloseSell, ActionCloseAll, ActionCancel:
		// volume not required
	default:
		return nil, fmt.Errorf("unsupported action %q", action)
	}

	return cmd, nil
}
