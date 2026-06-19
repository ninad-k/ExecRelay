# Contributing to ExecRelay

Thanks for taking the time to contribute. This document covers everything
you need to know to make changes safely.

---

## Prerequisites

- **Go** ≥ 1.25 (matches `go.mod`)
- **Python** ≥ 3.12 (matches Dockerfile base images)
- **Node** ≥ 22 (matches `apps/portal-web/Dockerfile`)
- **Docker** + **Docker Compose** v2
- **pre-commit** (`pipx install pre-commit` or `pip install --user pre-commit`)
- **golang-migrate** CLI (`brew install golang-migrate`) — needed if you
  change schema

---

## First-time setup

```sh
git clone https://github.com/ninad-k/ExecRelay.git
cd ExecRelay

# Install pre-commit hooks (one-time per clone).
make install-hooks

# Bring up the foundation tier (Postgres, NATS, observability) so tests can
# run against real services if needed.
docker compose up -d

# Run all checks to verify the environment.
make check
```

---

## Day-to-day workflow

```sh
# 1. Branch from main (see "Branch naming" below).
git switch -c feat/add-foo

# 2. Make changes; pre-commit will format Go/Python on every commit.

# 3. Run the relevant tests locally.
go test -race ./apps/ingress/...        # for Go changes
docker compose run --rm migrate         # if you added a migration
pre-commit run --all-files              # full sweep before pushing

# 4. Push & open a PR.
git push -u origin feat/add-foo
gh pr create
```

---

## Branch naming

| Prefix | Use for |
|---|---|
| `feat/` | New user-facing or developer-facing capability |
| `fix/` | Bug fix |
| `chore/` | Refactor, dependency bump, formatting, no behaviour change |
| `docs/` | Documentation only |
| `test/` | Test infra only (CI, fixtures, harness) |
| `perf/` | Performance work with no behaviour change |
| `security/` | Security fix (use private channel for the fix itself; see [`SECURITY.md`](SECURITY.md)) |

Use kebab-case after the prefix: `feat/kill-switch-endpoint`,
`fix/ingress-503-on-empty-body`.

---

## Commit messages

Format:

```
<scope>: <imperative summary in 50 chars or fewer>

Optional body explaining *why*, wrapped at 72 chars. Reference
related issues / PRs.

Co-Authored-By: Name <email>   (if pair-programmed)
```

`<scope>` is the directory or component: `ingress`, `portal-api`, `migrations`,
`docs`, `ci`, `scripts`, etc.

Good:
```
ingress: add perimeter token gate and license audit

Defense in depth on top of the existing per-license HMAC/secret
auth. The audit emits a Prometheus gauge so an open license can
be alerted on instead of discovered the hard way.
```

Bad:
```
update stuff
```

The repo uses [conventional verbs](https://www.conventionalcommits.org/)
without strict enforcement — be clear, not formal.

---

## Pull requests

A PR should:

- **Stay focused.** One logical change per PR. Refactor PRs separate from
  feature PRs.
- **Pass CI.** Every per-app workflow + `pre-commit` + `shellcheck` + (if
  applicable) `migrations` + `powershell` should be green.
- **Update the docs.** If you change a public endpoint, update
  [`docs/api/`](docs/api/). If you add a Prometheus metric, update
  [`docs/observability.md`](docs/observability.md).
- **Include tests.** New Go code → table-driven tests next to the code.
  New Python code → at minimum, extend the smoke test or add a real
  pytest case under `apps/<service>/tests/`.
- **Run `make lint`** before requesting review. Pre-commit hooks catch
  most issues, but `make lint` catches a few extras (e.g.,
  `check-added-large-files`).

---

## CI

Every push and PR runs (paths-filtered so unrelated CIs don't fire):

| Workflow | Triggers on | What it checks |
|---|---|---|
| `app-<name>.yml` | `apps/<name>/**` + shared paths | Per-service: vet/lint, unit tests, Docker build |
| `ci-shared.yml` | `internal/**`, `packages/**`, `go.mod`, `go.sum` | Shared Go vet + race tests; docker-compose config validation |
| `pre-commit.yml` | Any file | `gofmt`, `ruff-format`, `gitleaks`, `check-yaml`, etc. |
| `shellcheck.yml` | `scripts/*.sh` | shellcheck of all bash scripts |
| `powershell.yml` | `scripts/*.ps1` | PSScriptAnalyzer + parse check on `windows-latest` |
| `migrations.yml` | `infra/migrations/**` | Applies every migration against a fresh Postgres |
| `ecr-push.yml` | Manual (`workflow_dispatch`) | Builds + pushes images to ECR (once AWS is wired up) |

If a CI fails on a PR, the failing job's logs are the source of truth.
Common gotchas:

- **gofmt failure** — run `gofmt -w .` locally.
- **shellcheck failure** — open the script in your editor; shellcheck
  diagnostics include the line number and fix suggestion.
- **ruff failure** — pre-commit's `ruff-format` is what we enforce. The lint
  rules are intentionally off until existing E702/F841/E722 findings are
  triaged (see `.pre-commit-config.yaml`).

---

## Adding a database migration

```sh
make migrate-new NAME=add_user_preferences
# → creates infra/migrations/000004_add_user_preferences.{up,down}.sql

# Edit both files. The down file should reverse the up; if the change is
# inherently destructive, leave the down as `SELECT 1` and explain why in a
# comment (see 000001_foundation.down.sql for the pattern).

# Apply locally:
make migrate-up

# Verify the schema looks right:
docker compose exec postgres psql -U execrelay -d execrelay -c "\d+ user_preferences"
```

CI's `migrations.yml` workflow runs every migration against a fresh Postgres
on PR. If your migration fails there, it'll fail in production too.

After the migration ships, update [`docs/data-model.md`](docs/data-model.md)
with the new table's purpose.

---

## Adding a Prometheus metric

1. Declare the metric in `apps/<service>/internal/<service>/metrics.go` (Go)
   or near the top of `apps/<service>/app.py` (Python).
2. Use a clear, namespaced name: `<service>_<noun>_<unit>` (e.g.,
   `ingress_webhook_duration_seconds`, `bridge_ea_connected_count`).
3. Add it to [`docs/observability.md`](docs/observability.md) with:
   - What it measures
   - Expected range / cardinality
   - Suggested alert (if any)
4. If it's a counter or histogram, ensure a sensible test exercises the
   increment path so the label cardinality doesn't surprise you in prod.

---

## Performance & latency rules

The ingress hot path has a **95 ms p99 target**. If you're touching code in
`apps/ingress/internal/ingress/` or `packages/parser-go/`:

- **No database writes** on the hot path. Use a NATS event consumer instead.
- **No external HTTP calls** on the hot path. Cache anything that requires one.
- **No `defer` for things that should run immediately.** Go's `defer` is fast
  but not free.
- **Add a benchmark** if your change might affect latency:
  `go test -bench=. -benchmem ./apps/ingress/...`

---

## Style

- **Go**: `gofmt` (enforced). Standard library style. Don't add deps without
  a good reason — every new dep is a CVE we'll eventually have to patch.
- **Python**: `ruff format` (enforced). FastAPI conventions. Type hints
  required on new public functions.
- **TypeScript** (portal-web): Next.js conventions. `npm run type-check` must
  pass.
- **SQL**: Lowercase keywords (`select` not `SELECT`) by team preference, but
  the existing schema uses uppercase — stay consistent with the file you're
  editing.
- **Comments**: Explain *why*, not *what*. The code already says *what*.
- **Doc comments**: Public functions and exported types in Go get doc comments
  starting with the symbol name. Python uses docstrings on public callables.

---

## Code review

We aim to first-respond within 1 business day. Reviewers will:

- Run the change locally if it's anything beyond trivial.
- Check tests cover the change.
- Look for regressions in adjacent code paths.
- Ask "what happens if this fails?" — silent failures are the most common
  pre-merge issue.

Authors are expected to:

- Address every comment (resolve, fix, or push back with reasoning).
- Squash fixup commits before merge if there are more than a few.

---

## Releases

ExecRelay follows Semantic Versioning (`vX.Y.Z`) for tags.
- **CI on every push/PR:** the per-service workflows (`.github/workflows/app-*.yml`) and `ci.yml` build and test each service on every change to `main` and on PRs.
- **Docker Images → ECR:** image publishing is **manual today** — the `ecr-push.yml` workflow is `workflow_dispatch` only and tags images with the commit SHA (`github.sha`), not a semver tag. Automating an ECR push on tag creation is not yet wired up.
- **Changelog:** All changes are documented in `CHANGELOG.md` following the Keep-A-Changelog format.

---

## Code of Conduct

Be respectful, assume good intent, focus on the work. Disagreements about
design are normal and healthy; personal attacks are not.

For anything you'd rather not raise in public, email
`conduct@reycapitalsfo.com`.
