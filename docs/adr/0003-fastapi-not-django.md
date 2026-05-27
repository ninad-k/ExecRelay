# 3. Use FastAPI for cold-path Python services

Date: 2026-05-27
Status: Accepted

## Context

The cold-path services (`portal-api`, `analytics`, `reports`, `tasks`,
`persist`, `risk`, etc.) are written in Python. They need:

- REST APIs with JSON request/response
- Async I/O (NATS subscriptions + Postgres queries concurrently)
- Type-checked request models
- OpenAPI / Swagger docs out of the box
- Fast startup (matters for container deploys, k8s rolling updates)

Candidates:

| | FastAPI | Django | Flask |
|---|---|---|---|
| Async support | Native, first-class | Added 4.x, still rough in places | Add-on (Quart fork; sync core) |
| Request validation | Pydantic models, type-hinted | DRF serializers (verbose) | Marshmallow (separate dep) |
| OpenAPI / Swagger | Auto-generated at `/docs` | DRF + spectacular addon | Add-on |
| Startup time | <1 s | 2–5 s with app loaded | <1 s |
| Batteries | Minimal | Full (ORM, admin, auth, etc.) | Minimal |
| ORM | None bundled (we use asyncpg) | Django ORM (sync; not great for our load) | None bundled |

We don't need Django's "batteries included" — we don't have an admin
UI requirement, we're using TimescaleDB directly with async queries
via `asyncpg` (no ORM), and our auth is custom JWT (not Django's
session auth).

We do need first-class async because every service connects to NATS
and Postgres concurrently.

## Decision

Use FastAPI as the HTTP framework for all cold-path Python services.

Standardize on:

- `uvicorn` as the ASGI server.
- `asyncpg` for Postgres (with a connection pool).
- `pydantic` for request/response models.
- `Depends(get_pool)` / `Depends(current_user)` for shared resources.
- Routes annotated with `response_model=` so OpenAPI docs are accurate.

## Consequences

**Positive**

- Cold-path services start in <1 s — fast rolling updates.
- Auto-generated OpenAPI at `/docs` doubles as living API docs (the
  human-friendly version is [`docs/api/portal-api.md`](../api/portal-api.md)).
- Pydantic models catch malformed input at the framework boundary —
  fewer in-route validation lines.
- Async-by-default matches how every service uses the network — no
  thread-pool tuning, no GIL surprises with blocking calls.

**Negative**

- No batteries — we wrote our own auth, RBAC, audit logging instead of
  taking Django's. Net positive (less stuff we don't need) but it does
  mean we have to maintain those layers ourselves.
- Younger framework; fewer Stack Overflow answers for esoteric
  problems than Django.
- The cold-path Python files have grown large (portal-api is ~1300 lines).
  At some point we should split into routers (`apps/portal-api/routers/`)
  to keep editing manageable.
