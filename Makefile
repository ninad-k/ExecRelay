SHELL := /bin/sh
export GOCACHE := $(CURDIR)/.cache/go-build

GO_SERVICES := ingress bridge dxtrade
PY_SERVICES := persist portal-api tasks analytics reports
WEB_SERVICES := portal-web

.PHONY: check test bench compose-config docker-build loadtest loadtest-suite up down ps install-hooks lint

# Install pre-commit hooks (one-time per clone). Requires:
#   pipx install pre-commit  (or: pip install --user pre-commit)
install-hooks:
	pre-commit install
	@echo "pre-commit hooks installed; run 'make lint' to check the whole tree"

# Run all pre-commit hooks against every tracked file
lint:
	pre-commit run --all-files

# ---- Database migrations (golang-migrate) ----
# Install CLI:  brew install golang-migrate
MIGRATE_PATH := infra/migrations
MIGRATE_DB   ?= postgres://$(or $(POSTGRES_USER),execrelay):$(or $(POSTGRES_PASSWORD),execrelay_dev_password)@localhost:5432/$(or $(POSTGRES_DB),execrelay)?sslmode=disable
N            ?= 1

.PHONY: migrate-up migrate-down migrate-status migrate-new

migrate-up:
	migrate -path $(MIGRATE_PATH) -database "$(MIGRATE_DB)" up

migrate-down:
	migrate -path $(MIGRATE_PATH) -database "$(MIGRATE_DB)" down $(N)

migrate-status:
	migrate -path $(MIGRATE_PATH) -database "$(MIGRATE_DB)" version

migrate-new:
	@if [ -z "$(NAME)" ]; then echo "Usage: make migrate-new NAME=<short_description>"; exit 1; fi
	migrate create -ext sql -dir $(MIGRATE_PATH) -seq $(NAME)


check: compose-config test bench docker-build

# Run against a live ingress. Requires: make up first.
# Override defaults: make loadtest TARGET=http://... RATE=200 DURATION=60s
LOADTEST_TARGET  ?= http://localhost:8081/webhook
LOADTEST_RATE    ?= 50
LOADTEST_DUR     ?= 30s
LOADTEST_WORKERS ?= 10
loadtest:
	go run ./loadtest/cmd/loadtest \
		-target $(LOADTEST_TARGET) \
		-rate $(LOADTEST_RATE) \
		-duration $(LOADTEST_DUR) \
		-workers $(LOADTEST_WORKERS)

# Run comprehensive load test suite at multiple rates
LOADTEST_SUITE_TARGET ?= http://localhost:8081/webhook
LOADTEST_SUITE_OUT    ?= loadtest-results.txt
loadtest-suite:
	go run ./loadtest/cmd/loadtest-suite \
		-target $(LOADTEST_SUITE_TARGET) \
		-output $(LOADTEST_SUITE_OUT)
	@cat $(LOADTEST_SUITE_OUT)

test:
	go test ./...

bench:
	go test -bench=. -benchmem ./...

compose-config:
	docker compose config >/dev/null

docker-build:
	@for service in $(GO_SERVICES); do \
		docker build -f apps/$$service/Dockerfile -t execrelay/$$service:phase0 .; \
	done
	@for service in $(PY_SERVICES); do \
		docker build -f apps/$$service/Dockerfile -t execrelay/$$service:phase0 .; \
	done
	@for service in $(WEB_SERVICES); do \
		docker build -f apps/$$service/Dockerfile -t execrelay/$$service:phase0 .; \
	done

up:
	docker compose up -d

down:
	docker compose down

ps:
	docker compose ps
