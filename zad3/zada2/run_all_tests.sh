#!/bin/bash
set -eu
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVER_DIR="$SCRIPT_DIR/server"
CLIENT_DIR="$SCRIPT_DIR/client"
SERVER_PID=""

start_server() {
    local cfg="$1"
    pushd "$SERVER_DIR" >/dev/null
    if [ -n "$cfg" ]; then
        mvn -q exec:java -Dexec.mainClass=edu.sub.Main -Dexec.args="$cfg" >/tmp/server.log 2>&1 &
    else
        mvn -q exec:java -Dexec.mainClass=edu.sub.Main >/tmp/server.log 2>&1 &
    fi
    SERVER_PID=$!
    popd >/dev/null
    for i in $(seq 1 60); do
        if ss -tlnp 2>/dev/null | grep -q 50051; then
            return 0
        fi
        sleep 1
    done
    echo "ERROR: server did not bind 50051 in 60s"
    return 1
}

stop_server() {
    if [ -n "${SERVER_PID:-}" ]; then
        pkill -P "$SERVER_PID" 2>/dev/null || true
        kill "$SERVER_PID" 2>/dev/null || true
    fi
    pkill -f 'edu.sub.Main' 2>/dev/null || true
    SERVER_PID=""
    sleep 2
}

trap stop_server EXIT INT TERM

echo "==> compile server"
pushd "$SERVER_DIR" >/dev/null
mvn -q compile
popd >/dev/null

echo "==> phase 1: e2e suite (tests 1-11) with default config (TTL=60s)"
start_server ""
echo "  server up"
pushd "$CLIENT_DIR" >/dev/null
PYTHONUNBUFFERED=1 .venv/bin/python test_e2e.py
popd >/dev/null
echo "  phase 1 OK"
stop_server

echo "==> phase 2: TTL expiration test with config-test.properties (TTL=8s)"
start_server "config-test.properties"
echo "  server up with TTL=8s"
pushd "$CLIENT_DIR" >/dev/null
PYTHONUNBUFFERED=1 .venv/bin/python test_e2e.py ttl
popd >/dev/null
echo "  phase 2 OK"
stop_server

echo "==> phase 3: TUI smoke test (no server, mocked session)"
pushd "$CLIENT_DIR" >/dev/null
PYTHONUNBUFFERED=1 .venv/bin/python test_tui_smoke.py
popd >/dev/null
echo "  phase 3 OK"

echo
echo "ALL PHASES PASSED"
