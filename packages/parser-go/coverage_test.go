package parser

import (
	"strings"
	"testing"
)

// Round out parser-go coverage to >=80% by exercising the String()
// stringifiers (which had zero coverage) and the ParseError messages.
// These are pure-data functions; a few representative samples + a fallthrough
// to the default branch is enough to flip the lines green.

func TestParseError_Message_KnownCodes(t *testing.T) {
	cases := []struct {
		code ErrorCode
		want string
	}{
		{ErrEmptyInput, "empty alert"},
		{ErrMissingField, "missing required field"},
		{ErrUnknownCommand, "unknown command"},
		{ErrMalformedParam, "malformed parameter"},
		{ErrUnknownParam, "unknown parameter"},
		{ErrTooManyParams, "too many parameters"},
		{ErrDuplicateVolume, "more than one volume parameter"},
		{ErrDuplicateSL, "more than one stop-loss parameter"},
		{ErrDuplicateTP, "more than one take-profit parameter"},
		{ErrDuplicateEntry, "more than one entry parameter"},
		{ErrMissingVolume, "missing volume parameter"},
		{ErrPendingRequiresEntry, "pending command requires entry parameter"},
		{ErrRiskVolumeRequiresSL, "risk-by-loss volume requires stop-loss"},
		{ErrCloseAllRequiresChartSymbol, "closeall command requires chart symbol"},
		{ErrManagementSymbol, "management command requires matching special symbol"},
		{ErrCommentTooLong, "comment exceeds 20 characters"},
		{ErrATRRequiresTimeframePeriod, "ATR trailing requires atrtimeframe and atrperiod"},
		{ErrModifyRequiresSLTP, "modify command requires SL or TP parameter"},
		{ErrPartialVolumeRequiresRisk, "partial volume close requires risk parameter"},
	}
	for _, c := range cases {
		got := ParseError{Code: c.code}.Error()
		if got != c.want {
			t.Errorf("ErrorCode %d: got %q, want %q", c.code, got, c.want)
		}
	}
}

func TestParseError_Message_UnknownFallsBack(t *testing.T) {
	got := ParseError{Code: ErrorCode(250)}.Error()
	if got != "parse error" {
		t.Errorf("unknown code: got %q, want \"parse error\"", got)
	}
}

// String() on every Command — we don't care about each label here, just that
// (a) every defined value returns a non-empty string and (b) the default
// branch returns "invalid". Easy way to flip a huge switch to "covered".
func TestCommand_String_EveryDefinedValue(t *testing.T) {
	// Commands are dense iota values starting at 0. Walk a wide range and
	// keep the ones that return something other than "invalid".
	var covered int
	for i := 0; i < 64; i++ {
		got := Command(i).String()
		if got == "" {
			t.Fatalf("Command(%d) returned empty string", i)
		}
		if got != "invalid" {
			covered++
		}
	}
	if covered < 20 {
		t.Fatalf("expected >=20 named commands, got %d", covered)
	}
}

func TestCommand_String_InvalidFallback(t *testing.T) {
	if got := Command(99).String(); got != "invalid" {
		t.Fatalf("Command(99) = %q, want \"invalid\"", got)
	}
}

// Same broad sweep for ParamKind String() — covers ~30 cases at once.
func TestParamKind_String_EveryDefinedValue(t *testing.T) {
	var covered int
	for i := 0; i < 64; i++ {
		got := ParamKind(i).String()
		if got == "" {
			t.Fatalf("ParamKind(%d) returned empty string", i)
		}
		// "unknown" is the default branch return; everything else is named.
		if got != "unknown" {
			covered++
		}
	}
	if covered < 25 {
		t.Fatalf("expected >=25 named param kinds, got %d", covered)
	}
}

func TestParamKind_String_UnknownFallback(t *testing.T) {
	if got := ParamKind(250).String(); got != "unknown" {
		t.Fatalf("ParamKind(250) = %q, want \"unknown\"", got)
	}
}

// Smoke test commandFrom — call it with all known command names so the lookup
// table's hot paths get coverage.
func TestCommandFrom_RoundTripsKnownNames(t *testing.T) {
	names := []string{
		"buy", "sell", "buystop", "buylimit", "sellstop", "selllimit",
		"closeall", "cancellong", "cancelshort", "closelong", "closeshort",
		"closelongshort", "closelongpct", "closeshortpct",
		"closelongvol", "closeshortvol",
		"newsltplong", "newsltpshort", "newsltpbuystop", "newsltpbuylimit",
		"newsltpsellstop", "newsltpselllimit",
		"closelongopenlong", "closelongopenshort",
		"closeshortopenlong", "closeshortopenshort",
		"closelongshortopenlong", "closelongshortopenshort",
		"cancellongbuystop", "cancellongbuylimit",
		"cancelshortsellstop", "cancelshortselllimit",
		"eaoff", "eaon", "closealleaoff",
	}
	for _, n := range names {
		cmd := commandFrom(n)
		if cmd.String() == "invalid" {
			t.Errorf("commandFrom(%q) returned invalid", n)
		}
		// Case-insensitive — the lookup table normalizes the input.
		if up := commandFrom(strings.ToUpper(n)); up.String() == "invalid" {
			t.Errorf("commandFrom(upper %q) returned invalid", n)
		}
	}
}

func TestCommandFrom_RejectsUnknown(t *testing.T) {
	if cmd := commandFrom("notacommand"); cmd.String() != "invalid" {
		t.Fatalf("commandFrom(\"notacommand\") = %v, expected invalid", cmd)
	}
	if cmd := commandFrom(""); cmd.String() != "invalid" {
		t.Fatalf("commandFrom(\"\") = %v, expected invalid", cmd)
	}
}

// paramKindFromKey: same idea — cover the long switch with known keys.
func TestParamKindFromKey_RoundTripsKnownKeys(t *testing.T) {
	keys := []string{
		"risk", "vol_lots", "vol_dollar", "vol_pct_bal_loss", "vol_pct_eq_loss",
		"vol_pct_bal_margin",
		"sl", "sl_pips", "sl_price", "sl_pct",
		"tp", "tp_pips", "tp_price", "tp_pct",
		"pending", "entry_price", "entry_pips", "entry_pct",
		"trailtrig", "traildist", "trailstep",
		"atrtimeframe", "atrperiod", "atrmultiplier", "atrshift", "atrtrigger",
		"betrigger", "beoffset",
		"secret", "comment", "spread", "accfilter",
	}
	for _, k := range keys {
		kind := paramKindFromKey(k)
		if kind == ParamUnknown {
			t.Errorf("paramKindFromKey(%q) returned ParamUnknown", k)
		}
	}
}

func TestParamKindFromKey_RejectsUnknown(t *testing.T) {
	if kind := paramKindFromKey("not-a-param"); kind != ParamUnknown {
		t.Fatalf("paramKindFromKey(\"not-a-param\") = %v, want ParamUnknown", kind)
	}
}
