package parser

import "testing"

func TestParseMarketOrder(t *testing.T) {
	signal, err := Parse("60123456789,buy,EURUSD,vol_lots=0.1,sl_pips=20,tp_pips=40,secret=abc,comment=Breakout")
	if err != nil {
		t.Fatalf("Parse() error = %v", err)
	}
	if signal.LicenseID != "60123456789" {
		t.Fatalf("LicenseID = %q", signal.LicenseID)
	}
	if signal.Command != CommandBuy {
		t.Fatalf("Command = %s", signal.Command)
	}
	if signal.Symbol != "EURUSD" {
		t.Fatalf("Symbol = %q", signal.Symbol)
	}
	if signal.ParamCount != 5 {
		t.Fatalf("ParamCount = %d", signal.ParamCount)
	}
	if p, ok := signal.Param(ParamVolLots); !ok || p.Value != "0.1" {
		t.Fatalf("vol_lots param = %#v, %v", p, ok)
	}
}

func TestParseAliases(t *testing.T) {
	tests := []struct {
		raw  string
		want Command
	}{
		{"60123456789,long,US30.Cash,vol_lots=1", CommandBuy},
		{"60123456789,BULLISH,GBPJPY,vol_lots=1", CommandBuy},
		{"60123456789,short,DAX30,vol_lots=1", CommandSell},
		{"60123456789,bearish,EURUSD,vol_lots=1", CommandSell},
		{"60123456789,CL+OL,EURUSD,vol_lots=1", CommandCloseLongOpenLong},
		{"60123456789,CS+OS,EURUSD,risk=1", CommandCloseShortOpenShort},
		{"60123456789,CLS+OL,EURUSD,risk=1", CommandCloseLongShortOpenLong},
		{"60123456789,CLS+OS,EURUSD,risk=1", CommandCloseLongShortOpenShort},
	}

	for _, tt := range tests {
		signal, err := Parse(tt.raw)
		if err != nil {
			t.Fatalf("Parse(%q) error = %v", tt.raw, err)
		}
		if signal.Command != tt.want {
			t.Fatalf("Parse(%q) command = %s, want %s", tt.raw, signal.Command, tt.want)
		}
	}
}

func TestParsePendingOrder(t *testing.T) {
	signal, err := Parse("60123456789, buystop, EURUSD, entry_pips=20, vol_lots=0.1, sl_pips=10")
	if err != nil {
		t.Fatalf("Parse() error = %v", err)
	}
	if signal.Command != CommandBuyStop {
		t.Fatalf("Command = %s", signal.Command)
	}
	if !signal.HasParam(ParamEntryPips) {
		t.Fatal("entry_pips missing")
	}

	legacy, err := Parse("60123456789,cancellongbuystop,EURUSD,price=1.125,risk=1,sl=20")
	if err != nil {
		t.Fatalf("legacy price Parse() error = %v", err)
	}
	if !legacy.HasParam(ParamEntryPrice) {
		t.Fatal("legacy price param missing")
	}
}

func TestParseManagementCommands(t *testing.T) {
	if _, err := Parse("60123456789,eaoff,eaoff"); err != nil {
		t.Fatalf("eaoff error = %v", err)
	}
	if _, err := Parse("60123456789,eaon,EURUSD"); codeOf(err) != ErrManagementSymbol {
		t.Fatalf("eaon wrong symbol error = %v, want %v", err, ErrManagementSymbol)
	}
}

func TestParseModifyCommands(t *testing.T) {
	if _, err := Parse("60123456789,newsltplong,EURUSD,sl_pips=10,tp_pips=30"); err != nil {
		t.Fatalf("modify error = %v", err)
	}
	if _, err := Parse("60123456789,newsltpbuystop,EURUSD,sl_pips=20"); err != nil {
		t.Fatalf("pending modify error = %v", err)
	}
}

func TestParseRejects(t *testing.T) {
	tests := []struct {
		name string
		raw  string
		code ErrorCode
	}{
		{"empty", "", ErrEmptyInput},
		{"unknown command", "60123456789,hold,EURUSD", ErrUnknownCommand},
		{"unknown param", "60123456789,buy,EURUSD,vol_lots=1,foo=bar", ErrUnknownParam},
		{"duplicate volume", "60123456789,buy,EURUSD,vol_lots=1,risk=1", ErrDuplicateVolume},
		{"duplicate sl", "60123456789,buy,EURUSD,vol_lots=1,sl=10,sl_pips=10", ErrDuplicateSL},
		{"duplicate tp", "60123456789,buy,EURUSD,vol_lots=1,tp=10,tp_pips=10", ErrDuplicateTP},
		{"pending entry", "60123456789,buylimit,EURUSD,vol_lots=1", ErrPendingRequiresEntry},
		{"vol dollar sl", "60123456789,sell,EURUSD,vol_dollar=50", ErrRiskVolumeRequiresSL},
		{"comment length", "60123456789,buy,EURUSD,vol_lots=1,comment=123456789012345678901", ErrCommentTooLong},
		{"atr incomplete", "60123456789,buy,EURUSD,vol_lots=1,atrperiod=14", ErrATRRequiresTimeframePeriod},
		{"modify missing sltp", "60123456789,newsltplong,EURUSD,spread=1", ErrModifyRequiresSLTP},
		{"close vol missing risk", "60123456789,closelongvol,EURUSD", ErrPartialVolumeRequiresRisk},
		{"open missing volume", "60123456789,buy,EURUSD", ErrMissingVolume},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			_, err := Parse(tt.raw)
			if codeOf(err) != tt.code {
				t.Fatalf("Parse() error = %v, code = %v, want %v", err, codeOf(err), tt.code)
			}
		})
	}
}

func TestParseATRComplete(t *testing.T) {
	_, err := Parse("60123456789,buy,EURUSD,vol_lots=1,sl_pips=10,atrtimeframe=60,atrperiod=14,atrmultiplier=2,atrtrigger=8")
	if err != nil {
		t.Fatalf("Parse() error = %v", err)
	}
}

func TestParseAllocationBudget(t *testing.T) {
	raw := "60123456789,buy,EURUSD,vol_lots=1,sl_pips=10,tp_pips=20,trailtrig=15,traildist=10,trailstep=3,betrigger=30,beoffset=5,spread=2"
	allocs := testing.AllocsPerRun(1000, func() {
		if _, err := Parse(raw); err != nil {
			t.Fatal(err)
		}
	})
	if allocs > 2 {
		t.Fatalf("allocs = %.2f, want <= 2", allocs)
	}
}

func BenchmarkParseMarketOrder(b *testing.B) {
	raw := "60123456789,buy,EURUSD,vol_lots=1,sl_pips=10,tp_pips=20,trailtrig=15,traildist=10,trailstep=3,betrigger=30,beoffset=5,spread=2"
	for i := 0; i < b.N; i++ {
		if _, err := Parse(raw); err != nil {
			b.Fatal(err)
		}
	}
}

func BenchmarkParsePendingOrder(b *testing.B) {
	raw := "60123456789,buystop,EURUSD,entry_pips=20,vol_lots=0.1,sl_pips=10,tp_pips=30,secret=abc"
	for i := 0; i < b.N; i++ {
		if _, err := Parse(raw); err != nil {
			b.Fatal(err)
		}
	}
}

func codeOf(err error) ErrorCode {
	if err == nil {
		return 0
	}
	if parseErr, ok := err.(ParseError); ok {
		return parseErr.Code
	}
	return 0
}
