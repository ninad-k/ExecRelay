# Database migrations

Managed by [golang-migrate/migrate](https://github.com/golang-migrate/migrate).
Each version is a pair of files:

```
000004_add_user_settings.up.sql     # forward
000004_add_user_settings.down.sql   # reverse (or intentionally empty)
```

## Running migrations

**Local (docker compose):** the `migrate` service runs automatically on
`docker compose up` and applies any pending migrations before the app
services start.

**From the host (requires `migrate` CLI installed via `brew install golang-migrate`):**

```
make migrate-up                # apply all pending
make migrate-down N=1          # roll back N steps
make migrate-status            # show current version
make migrate-new NAME=foo      # generate timestamped 00000N_foo.up.sql + .down.sql
```

## Down migrations

The seed migrations (000001-000003) ship with **intentionally empty down
files**, because rolling back the foundation schema would drop every user,
license, fill, and audit record. For real schema changes, prefer writing a
new forward migration (e.g. `000007_drop_legacy_table.up.sql`) instead of
relying on `migrate down`.

## Bringing an existing DB under management

If a Postgres instance was initialized before this migration framework
existed (e.g. via the old `init/` mount), mark migrations 1-3 as already
applied without re-running them:

```
migrate -path infra/migrations -database "$DATABASE_URL" force 3
```
