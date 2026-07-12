#!/usr/bin/env bash
# Run the ExecRelay core stack natively (no Docker): NATS + ml-predictor +
# ingress + bridge. Useful on machines without Docker (e.g. Windows + Git
# Bash) and for live EA testing against a local MT5 terminal.
#
#   scripts/local-stack.sh start    # build + start everything, wait healthy
#   scripts/local-stack.sh stop     # stop everything started by this script
#   scripts/local-stack.sh status   # health-check each component
#
# Ports: NATS 4222, ml-predictor 8080, ingress 8081, bridge 8082 (the MT5
# EA's default). Override via env before calling start.
#
# Requires: go, python (with apps/ml-predictor deps installed), curl, unzip.
# nats-server is downloaded automatically into .local-stack/ if missing.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIR="$ROOT/.local-stack"
BIN="$DIR/bin"
LOGS="$DIR/logs"
PIDS="$DIR/pids"
NATS_VERSION="${NATS_VERSION:-2.12.1}"

NATS_PORT="${NATS_PORT:-4222}"
PREDICTOR_PORT="${PREDICTOR_PORT:-8080}"
INGRESS_PORT="${INGRESS_PORT:-8081}"
BRIDGE_PORT="${BRIDGE_PORT:-8082}"

# Dev-only defaults; override for anything beyond local testing.
export EXECRELAY_LICENSES="${EXECRELAY_LICENSES:-60000000001:test-secret:test-hmac-secret:test-instance:mt5}"
export BRIDGE_AUTH_TOKEN="${BRIDGE_AUTH_TOKEN:-test-bridge-token}"
export ML_ENFORCE="${ML_ENFORCE:-false}"
export ML_THRESHOLD="${ML_THRESHOLD:-0.50}"

exe_suffix=""
case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) exe_suffix=".exe" ;; esac

nats_bin() {
    local sys="linux-amd64"
    case "$(uname -s)" in
        MINGW*|MSYS*|CYGWIN*) sys="windows-amd64" ;;
        Darwin) sys="darwin-$(uname -m | sed 's/x86_64/amd64/')" ;;
    esac
    local dir="$DIR/nats-server-v${NATS_VERSION}-${sys}"
    if [ ! -x "$dir/nats-server$exe_suffix" ]; then
        echo "downloading nats-server v$NATS_VERSION..." >&2
        mkdir -p "$DIR"
        curl -sSL --retry 3 -o "$DIR/nats.zip" \
            "https://github.com/nats-io/nats-server/releases/download/v${NATS_VERSION}/nats-server-v${NATS_VERSION}-${sys}.zip"
        unzip -q -o "$DIR/nats.zip" -d "$DIR"
        rm -f "$DIR/nats.zip"
    fi
    echo "$dir/nats-server$exe_suffix"
}

start_one() { # name, log, pidfile, cmd...
    local name="$1" log="$2" pidfile="$3"
    shift 3
    "$@" >"$log" 2>&1 &
    echo $! >"$pidfile"
    echo "  $name: pid $(cat "$pidfile") (log: $log)"
}

wait_http() { # name, url, tries
    local name="$1" url="$2" tries="${3:-30}"
    for _ in $(seq 1 "$tries"); do
        if curl -s -o /dev/null --max-time 2 "$url"; then
            echo "  $name: healthy ($url)"
            return 0
        fi
        sleep 1
    done
    echo "  $name: FAILED to become healthy ($url) — check logs in $LOGS" >&2
    return 1
}

cmd_start() {
    mkdir -p "$BIN" "$LOGS" "$PIDS"
    local nats
    nats="$(nats_bin)"

    echo "building services..."
    (cd "$ROOT" && go build -o "$BIN/ingress$exe_suffix" ./apps/ingress/cmd/ingress)
    (cd "$ROOT" && go build -o "$BIN/bridge$exe_suffix" ./apps/bridge/cmd/bridge)

    echo "starting stack..."
    start_one "nats" "$LOGS/nats.log" "$PIDS/nats.pid" \
        "$nats" -js -p "$NATS_PORT"

    (cd "$ROOT/apps/ml-predictor" && HTTP_PORT="$PREDICTOR_PORT" DEBUG=false \
        python app.py >"$LOGS/ml-predictor.log" 2>&1 & echo $! >"$PIDS/ml-predictor.pid")
    echo "  ml-predictor: pid $(cat "$PIDS/ml-predictor.pid") (log: $LOGS/ml-predictor.log)"

    HTTP_ADDR=":$INGRESS_PORT" \
    NATS_URL="nats://127.0.0.1:$NATS_PORT" \
    WEBHOOK_RATE_LIMIT="${WEBHOOK_RATE_LIMIT:-0}" \
    WEBHOOK_TIMESTAMP_WINDOW_SECS="${WEBHOOK_TIMESTAMP_WINDOW_SECS:-0}" \
    ML_PREDICTOR_URL="http://127.0.0.1:$PREDICTOR_PORT" \
    start_one "ingress" "$LOGS/ingress.log" "$PIDS/ingress.pid" \
        "$BIN/ingress$exe_suffix"

    HTTP_ADDR=":$BRIDGE_PORT" \
    NATS_URL="nats://127.0.0.1:$NATS_PORT" \
    start_one "bridge" "$LOGS/bridge.log" "$PIDS/bridge.pid" \
        "$BIN/bridge$exe_suffix"

    echo "waiting for health..."
    wait_http "ml-predictor" "http://127.0.0.1:$PREDICTOR_PORT/healthz"
    wait_http "ingress" "http://127.0.0.1:$INGRESS_PORT/health"
    wait_http "bridge" "http://127.0.0.1:$BRIDGE_PORT/health"
    echo "stack is up. EA connects to 127.0.0.1:$BRIDGE_PORT (instance: test-instance)."
}

cmd_stop() {
    local any=0
    for pidfile in "$PIDS"/*.pid; do
        [ -e "$pidfile" ] || continue
        any=1
        local pid name
        pid="$(cat "$pidfile")"
        name="$(basename "$pidfile" .pid)"
        if kill "$pid" 2>/dev/null; then
            echo "  stopped $name (pid $pid)"
        else
            echo "  $name (pid $pid) already gone"
        fi
        rm -f "$pidfile"
    done
    [ "$any" = 1 ] || echo "nothing to stop (no pid files in $PIDS)"
}

cmd_status() {
    curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$PREDICTOR_PORT/readyz" \
        && echo "ml-predictor: ready (:$PREDICTOR_PORT)" || echo "ml-predictor: DOWN"
    curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$INGRESS_PORT/health" \
        && echo "ingress: up (:$INGRESS_PORT)" || echo "ingress: DOWN"
    curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$BRIDGE_PORT/health" \
        && echo "bridge: up (:$BRIDGE_PORT)" || echo "bridge: DOWN"
}

case "${1:-}" in
    start) cmd_start ;;
    stop) cmd_stop ;;
    status) cmd_status ;;
    *) echo "usage: $0 start|stop|status" >&2; exit 2 ;;
esac
