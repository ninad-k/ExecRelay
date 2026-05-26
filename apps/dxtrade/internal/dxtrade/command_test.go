package dxtrade

import (
	"testing"
)

func TestParseCommand_buy(t *testing.T) {
	cmd, err := ParseCommand("trace1", "buy", "EUR/USD", map[string]string{"vol_lots": "0.1"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cmd.Action != ActionBuy || cmd.Symbol != "EUR/USD" || cmd.Volume != 0.1 || cmd.TraceID != "trace1" {
		t.Fatalf("unexpected command: %+v", cmd)
	}
}

func TestParseCommand_sell(t *testing.T) {
	cmd, err := ParseCommand("t", "sell", "GBP/USD", map[string]string{"vol_lots": "0.5"})
	if err != nil || cmd.Volume != 0.5 {
		t.Fatalf("got %v, %v", cmd, err)
	}
}

func TestParseCommand_pendingOrders(t *testing.T) {
	for _, action := range []string{"buystop", "sellstop", "buylimit", "selllimit"} {
		cmd, err := ParseCommand("t", action, "EUR/USD", map[string]string{"vol_lots": "1.0"})
		if err != nil {
			t.Fatalf("action %s: unexpected error: %v", action, err)
		}
		if cmd.Volume != 1.0 {
			t.Fatalf("action %s: wrong volume %v", action, cmd.Volume)
		}
	}
}

func TestParseCommand_closeActions_noVolumeRequired(t *testing.T) {
	for _, action := range []string{"closebuy", "closesell", "closeall", "cancel"} {
		cmd, err := ParseCommand("t", action, "EUR/USD", map[string]string{})
		if err != nil {
			t.Fatalf("action %s: unexpected error: %v", action, err)
		}
		if cmd.Volume != 0 {
			t.Fatalf("action %s: expected zero volume, got %v", action, cmd.Volume)
		}
	}
}

func TestParseCommand_missingVolume(t *testing.T) {
	_, err := ParseCommand("t", "buy", "EUR/USD", map[string]string{})
	if err == nil {
		t.Fatal("expected error for buy without vol_lots")
	}
}

func TestParseCommand_invalidVolume(t *testing.T) {
	_, err := ParseCommand("t", "buy", "EUR/USD", map[string]string{"vol_lots": "not-a-number"})
	if err == nil {
		t.Fatal("expected error for invalid vol_lots")
	}
}

func TestParseCommand_zeroVolume(t *testing.T) {
	_, err := ParseCommand("t", "buy", "EUR/USD", map[string]string{"vol_lots": "0"})
	if err == nil {
		t.Fatal("expected error for zero volume on buy")
	}
}

func TestParseCommand_negativeVolume(t *testing.T) {
	_, err := ParseCommand("t", "buy", "EUR/USD", map[string]string{"vol_lots": "-0.1"})
	if err == nil {
		t.Fatal("expected error for negative volume on buy")
	}
}

func TestParseCommand_unsupportedAction(t *testing.T) {
	_, err := ParseCommand("t", "modify", "EUR/USD", map[string]string{})
	if err == nil {
		t.Fatal("expected error for unsupported action")
	}
}

func TestParseCommand_extraParamsIgnored(t *testing.T) {
	cmd, err := ParseCommand("t", "buy", "EUR/USD", map[string]string{
		"vol_lots": "0.2",
		"sl_pips":  "20",
		"tp_pips":  "40",
		"comment":  "hello",
	})
	if err != nil || cmd.Volume != 0.2 {
		t.Fatalf("got %v, %v", cmd, err)
	}
}
