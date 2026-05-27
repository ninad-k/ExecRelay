# 2. Use NATS JetStream instead of Kafka

Date: 2026-05-27
Status: Accepted

## Context

The hot path (ingress → bridge / dxtrade / persist) needs a durable
pub/sub transport that:

- Has **sub-ms publish latency** on the producer side.
- Supports **at-least-once delivery** with durable consumer state so
  bridge can resume after a restart without losing in-flight signals.
- Has **subject-based routing** so we can route by
  `signals.<platform>.<licenseID>.<instanceID>` and let consumers
  subscribe to wildcards.
- Is **operationally cheap** for a small team to run on a single host
  (or single Kubernetes cluster) — no separate Zookeeper, no JVM heap
  tuning, no Kafka Connect.

The two obvious options:

| | NATS JetStream | Apache Kafka |
|---|---|---|
| Publish latency | <1 ms typical | 1–10 ms typical (broker → leader → ack) |
| Per-message routing | First-class subject wildcards | Topics + partition routing; subject-style requires custom |
| Operational footprint | Single binary (`nats-server`), one config file | JVM + Zookeeper (or KRaft); separate management of brokers + connect cluster |
| Durable consumers | Native (consumer state on broker) | Native (consumer offsets) |
| Replay / time-travel | Stream retention + replay from offset/timestamp | Same |
| Throughput ceiling | ~1M msg/sec on a single broker (more than we need) | 100k–10M msg/sec depending on config |
| Ecosystem | Smaller, focused on real-time messaging | Larger, includes streaming SQL, connectors |

Our throughput target is **thousands of signals per second peak**, not
hundreds of thousands. Latency matters more than throughput.

## Decision

Use NATS JetStream as the transport for both the hot path and event
streams.

Stream layout:

- `SIGNALS` stream, subjects `signals.>`, used by ingress (producer)
  and bridge / dxtrade / persist (durable consumers).
- `FILLS` stream, subjects `fills.>`, used by bridge / dxtrade
  (producers) and persist (consumer).
- `EVENTS` stream, subjects `events.>`, used for rejection events,
  license reloads, kill-switch toggles.

NATS user/password auth with separate accounts per environment;
mTLS-capable but not enabled today (see [`SECURITY.md`](../../SECURITY.md)).

## Consequences

**Positive**

- The hot path stays well under the 95 ms p99 budget; NATS adds 1–2 ms.
- Operational complexity is low — one container in `docker-compose.yml`,
  one Helm subchart.
- Subject wildcards make tenancy-aware fan-out trivial
  (`signals.mt5.>` for the MT5 bridge, `signals.dxtrade.>` for dxtrade).
- The single binary fits the single-server deployment model the rest
  of this codebase is built around.

**Negative**

- Smaller ecosystem than Kafka — no Kafka Connect equivalents for
  ingest/egress.
- JetStream's clustering story is less mature than Kafka's; multi-region
  super-clusters work but require more hand-holding. **For the current
  single-region deployment this doesn't matter; for the Phase 6
  multi-region roadmap we may revisit.**
- Fewer external tools / SaaS observability integrations (Datadog,
  New Relic) have first-class NATS support compared to Kafka.

## Notes for future ADRs

If the throughput requirement grows past 100k signals/sec sustained, or
the multi-region story becomes a hard requirement and JetStream's
operational complexity grows past Kafka's, write a successor ADR.
Re-evaluation triggers:

- Sustained > 50k signals/sec across all licenses
- More than two regions in active production
- An operational incident attributable to JetStream's clustering
