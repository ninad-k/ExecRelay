# Architecture Decision Records (ADRs)

An ADR captures the **context, decision, and consequences** of a single
architectural choice, in a one-page document. ADRs are immutable once
merged — if a decision is reversed, write a new ADR that supersedes the
old one rather than editing history.

Format: [Michael Nygard's template](https://github.com/joelparkerhenderson/architecture-decision-record/blob/main/locales/en/templates/decision-record-template-by-michael-nygard/index.md).

## Index

| # | Title | Status |
|---|---|---|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | Accepted |
| [0002](0002-nats-jetstream-not-kafka.md) | Use NATS JetStream instead of Kafka | Accepted |
| [0003](0003-fastapi-not-django.md) | Use FastAPI for cold-path Python services | Accepted |
| [0004](0004-hand-rolled-signal-pb-go.md) | Hand-roll `signal.pb.go` rather than generate from `.proto` | Accepted |
| [0005](0005-golang-migrate-for-schema.md) | Use `golang-migrate` for DB schema management | Accepted |
| [0006](0006-per-license-hmac-not-global.md) | Per-license HMAC + body secret instead of a global perimeter token (perimeter is *additional*) | Accepted |
| [0007](0007-wsl2-for-windows-deployment.md) | Deploy on Windows Server via WSL2 instead of native Windows containers | Accepted |

## How to add a new ADR

1. Copy the most recent ADR file as a template.
2. Number it sequentially.
3. Title: short verb phrase, kebab-case in the filename.
4. Status starts as **Proposed**; flips to **Accepted** when merged.
5. If superseding an existing ADR, set its status to **Superseded by NNNN**.
