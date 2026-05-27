package execrelaypb_test

import (
	"encoding/hex"
	"testing"

	oldproto "github.com/golang/protobuf/proto"
	execrelaypb "github.com/ninadk/execrelay/packages/proto"
)

// canonicalSignal is the fixed input for the wire-format golden. Do not change
// these field values without also recomputing the golden — the point of the
// test is to detect *unintended* changes to the on-wire format that would
// silently break the Python parsers in apps/persist, apps/portal-api, and
// any other consumer of signal.pb.go's binary representation.
func canonicalSignal() *execrelaypb.Signal {
	return &execrelaypb.Signal{
		TraceId:          "trace-fixture-0001",
		LicenseId:        "60123456789",
		InstanceId:       "mt5-a",
		Command:          "buy",
		RawCommand:       "BUY",
		Symbol:           "EURUSD",
		IngressRegion:    "iad",
		ReceivedUnixNano: 1700000000000000000,
		BodySha256:       "deadbeefcafef00d",
		Params: []*execrelaypb.SignalParam{
			{Key: "vol_lots", Value: "0.10"},
			{Key: "sl_pips", Value: "20"},
		},
	}
}

// canonicalSignalWireHex is the marshaled hex of canonicalSignal() at the time
// this test was written. If you intentionally change a Signal / SignalParam
// field tag, type, or numbering:
//
//  1. Update the matching field layout in apps/persist/app.py and any other
//     Python service that parses Signal wire bytes by field number.
//  2. Run: cd packages/proto && go test -run TestSignalWireFormat -v 2>&1
//     The failing test prints the new hex — copy it into this constant.
//  3. Note in your PR description that this is a wire-format break.
const canonicalSignalWireHex = "0a1274726163652d666978747572652d30303031120b36303132333435363738391a056d74352d6122036275792a0342555932064555525553443a03696164408080a8b1e39fe7cb174a106465616462656566636166656630306452100a08766f6c5f6c6f74731204302e3130520d0a07736c5f7069707312023230"

func TestSignalWireFormatGolden(t *testing.T) {
	got, err := oldproto.Marshal(canonicalSignal())
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	gotHex := hex.EncodeToString(got)
	if gotHex != canonicalSignalWireHex {
		t.Fatalf(
			"Signal wire format changed. Either revert the field-tag change OR\n"+
				"update Python consumers (apps/persist, apps/portal-api, ...) AND replace\n"+
				"canonicalSignalWireHex with the new value.\n\n"+
				"  expected: %s\n  got:      %s",
			canonicalSignalWireHex, gotHex,
		)
	}
}

func TestSignalRoundTrip(t *testing.T) {
	original := canonicalSignal()
	wire, err := oldproto.Marshal(original)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var decoded execrelaypb.Signal
	if err := oldproto.Unmarshal(wire, &decoded); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if decoded.TraceId != original.TraceId ||
		decoded.LicenseId != original.LicenseId ||
		decoded.InstanceId != original.InstanceId ||
		decoded.Command != original.Command ||
		decoded.Symbol != original.Symbol ||
		decoded.ReceivedUnixNano != original.ReceivedUnixNano ||
		decoded.BodySha256 != original.BodySha256 {
		t.Fatalf("round-trip lost a field. original=%+v decoded=%+v", original, &decoded)
	}
	if len(decoded.Params) != len(original.Params) {
		t.Fatalf("params count: got %d, want %d", len(decoded.Params), len(original.Params))
	}
	for i := range original.Params {
		if decoded.Params[i].Key != original.Params[i].Key ||
			decoded.Params[i].Value != original.Params[i].Value {
			t.Fatalf("params[%d] mismatch: got %+v, want %+v",
				i, decoded.Params[i], original.Params[i])
		}
	}
}
