# 5. Use `golang-migrate` for DB schema management

Date: 2026-05-27
Status: Accepted

## Context

Prior to this decision, schema lived in three SQL files mounted as
Postgres `/docker-entrypoint-initdb.d`:

- `infra/docker/postgres/init/001_foundation.sql`
- `infra/docker/postgres/init/002_features.sql`
- `infra/docker/postgres/init/003_phase6_correlation.sql`

These run **only** on first-init of an empty data volume. Re-running on
an existing volume does nothing. Every file uses `CREATE TABLE IF NOT
EXISTS` so re-execution would be safe, but there's no tracking of which
files have been applied — so on an existing DB you couldn't tell
whether `003` had been run, or whether a new file should be applied.

Real-world consequences:

- "Did the prod box ever get the `003` migration?" — no answer
  without manually inspecting the schema.
- New devs spin up clean and get everything; long-running envs miss
  features.
- Schema changes during dev get hand-applied with `psql` and forgotten.

We need a migration tool that:

- **Tracks which migrations have been applied** in a metadata table.
- Is **language-neutral** (the codebase is Go + Python + TypeScript;
  whichever tool we pick should not require a runtime that only one
  team uses).
- **Plain SQL files**, not a programmatic DSL (we don't want to learn
  Atlas's HCL or sqlboiler).
- **Single-binary CLI** that can also run as a Docker image in
  `docker-compose`.
- **Up + down** migrations supported, with the option to skip down
  files for destructive changes.

Candidates:

| | golang-migrate | Atlas | Liquibase | Flyway | sql-migrate |
|---|---|---|---|---|---|
| Plain SQL | yes | yes (or HCL) | XML / YAML / SQL | SQL | yes |
| Single-binary | yes | yes | JVM | JVM | yes |
| Active maintenance | yes | yes | yes | yes | quieter |
| Compose-friendly image | `migrate/migrate` | `arigaio/atlas` | yes | yes | self-build |
| Adoption | very widely used in Go | newer | very widely used in enterprise | similar | smaller |

`golang-migrate` is the canonical Go-ecosystem choice, has a small
Docker image, and uses plain SQL.

## Decision

Adopt `golang-migrate` v4. Each migration is a pair of files:

```
infra/migrations/000004_add_user_settings.up.sql
infra/migrations/000004_add_user_settings.down.sql
```

The compose stack runs a `migrate` service that calls
`migrate ... up` and exits 0; every app service `depends_on` it via
`service_completed_successfully`, so apps only start after schema is at
HEAD.

Migrations 000001–000003 are byte-for-byte copies of the previous init
scripts. The corresponding `.down.sql` files for these are intentionally
`SELECT 1` with a comment explaining that automatic rollback would
destroy production data; if a real rollback is needed, the operator
writes a new forward migration.

Local CLI usage:

```
make migrate-up
make migrate-down N=1
make migrate-status
make migrate-new NAME=add_user_settings
```

CI's `migrations.yml` applies every migration against a fresh Postgres
on PRs that touch `infra/migrations/`.

## Consequences

**Positive**

- Single source of truth for schema state (`schema_migrations` table).
- Adding a new migration is a documented one-line command + an editor.
- CI verifies the migration applies cleanly before merge.
- Existing prod can adopt the framework via `migrate force 3` (no
  re-execution of seed scripts).

**Negative**

- Plain SQL means you write deltas, not desired state — Atlas's HCL
  could be argued to be cleaner for very-large refactors. We accept
  this for the simpler tooling story.
- The `.down.sql` files for foundation migrations are stubs. Real
  reversal of a destructive change requires writing a new forward
  migration, not relying on `migrate down`. The team must internalise
  this — see `infra/migrations/README.md`.

## Notes for future ADRs

If schema complexity grows to the point that hand-editing SQL deltas
becomes error-prone (e.g., > 100 migrations, lots of cross-table
constraint refactors), revisit Atlas which can diff a desired-state HCL
spec and emit safe forward migrations.
