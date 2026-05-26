SHELL := /bin/sh
export GOCACHE := $(CURDIR)/.cache/go-build

GO_SERVICES := ingress bridge dxtrade
PY_SERVICES := persist portal-api tasks analytics reports
WEB_SERVICES := portal-web

.PHONY: check test bench compose-config docker-build loadtest up down ps

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
