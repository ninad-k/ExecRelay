# proto

Hand-rolled Go protobuf types for the hot-path wire format used between
ingress, bridge / dxtrade, persist, and the Python consumers in
`apps/persist/` and `apps/portal-api/`.

**There is no `.proto` source file.** See
[`docs/adr/0004-hand-rolled-signal-pb-go.md`](../../docs/adr/0004-hand-rolled-signal-pb-go.md)
for the reasoning. Any change to field tags, types, or numbers must
update the Python parsers in `apps/persist/app.py` and
`apps/portal-api/app.py` as well — the wire-format golden test in
`wire_test.go` will fail loudly if you forget.

## Types

| Message | Used for |
|---|---|
| `Signal` | One trade signal — license/instance/command/symbol + parameters + trace ID |
| `SignalParam` | Key/value parameter inside a Signal (`vol_lots=0.1`, etc.) |

See [`signal.pb.go`](signal.pb.go) for the field-level definitions and
their protobuf tags.

## Tests

`wire_test.go`:

- **`TestSignalWireFormatGolden`** — marshals a canonical Signal and
  asserts the bytes match a committed hex string. Detects accidental
  tag/type changes that would silently break Python parsers.
- **`TestSignalRoundTrip`** — marshal → unmarshal and assert every
  field is preserved. Catches accidental field deletions.

To intentionally change the wire format:

1. Update Signal / SignalParam in `signal.pb.go`.
2. Update every Python parser that decodes by field number (grep
   `string_fields`, `wire_type` in `apps/`).
3. Run `go test ./packages/proto/...` — copy the new hex into
   `canonicalSignalWireHex`.
4. Note in your PR description that this is a wire-format break.

## See also

- [`docs/ARCHITECTURE.md`](../../docs/ARCHITECTURE.md) — where Signal
  flows through the system
- [`docs/adr/0004-hand-rolled-signal-pb-go.md`](../../docs/adr/0004-hand-rolled-signal-pb-go.md)
  — why no `.proto`
