# 4. Hand-roll `signal.pb.go` rather than generate from `.proto`

Date: 2026-05-27
Status: Accepted

## Context

`packages/proto/signal.pb.go` defines the protobuf `Signal` and
`SignalParam` types used as the wire format between ingress, bridge,
dxtrade, and persist (Go side) plus the Python wire-format parsers in
`apps/persist/app.py` and `apps/portal-api/app.py`.

**There is no `.proto` source file.** The Go file is hand-written
using the same struct tags `protoc-gen-go` would emit, and the Python
parsers manually decode by field number.

This is unusual. The natural question is: should we generate
`signal.pb.go` from a `.proto` definition?

### Generated-from-proto: what we'd get

- Single source of truth (`.proto`).
- Automatic Python types via `protoc-gen-python` or `grpcio-tools`.
- Standard protobuf reflection support.
- `buf` ecosystem for breaking-change detection on PRs.

### Generated-from-proto: what we'd pay

- A protoc toolchain dependency in every dev's setup and in CI.
- Generated files become a build artefact — either checked in (and
  potentially out of sync) or regenerated on every build.
- Larger generated files; the current `signal.pb.go` is hand-tuned for
  zero-allocation marshalling in hot-path code.
- Python parsers in this codebase don't use the generated protobuf
  library — they hand-decode the wire format because the protobuf
  library is a Python wheel with C deps that's annoying inside
  `python:3.12-slim` containers and would add an extra serialization
  step.

The wire format itself is stable (the project rarely changes Signal
field shapes), so the maintenance burden of the hand-rolled approach is
genuinely low.

## Decision

Keep `packages/proto/signal.pb.go` hand-written. Defer protoc adoption
until one of:

- The Signal message grows significantly (more than ~20 fields).
- We add a new wire-format message type that's used in multiple
  services with different generated codegen needs.
- We adopt gRPC for service-to-service RPC (today we use NATS for
  everything).

**Guard the hand-rolled file** with the wire-format golden test added
in `packages/proto/wire_test.go`. Any change to field tags, types, or
numbers fails the golden test with a message pointing at the Python
parsers that need matching updates.

## Consequences

**Positive**

- No protoc toolchain dependency anywhere.
- Hand-tuned struct works for hot-path performance.
- Python parsers are simple, dependency-free wire decoders — easy to
  reason about and to port to other languages if needed.
- The golden test makes accidental wire-format breaks loud at PR time.

**Negative**

- Adding a new field requires updating *both* `signal.pb.go` and every
  Python parser that decodes by field number. The golden test will
  surface the divergence but it's still manual work.
- New engineers expect a `.proto` file and are confused by its absence
  — this ADR is the answer.
- Tooling that auto-generates clients (e.g., `buf generate` for TS)
  doesn't apply.

## Notes for future ADRs

If we add gRPC for service-to-service RPC, or if the Python services
gain real `protobuf` library usage (e.g., for new larger messages), it
will become natural to write the `.proto` file at that point and
*generate* the existing `signal.pb.go` from it. The golden test
guarantees a regression-free migration.
