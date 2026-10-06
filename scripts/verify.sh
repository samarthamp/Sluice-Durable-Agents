#!/usr/bin/env bash
# ============================================================================
#  Sluice -- one-command end-to-end verification (Linux / WSL2)
#
#    scripts/verify.sh              clean, start Redis + services, verify, tear down
#    scripts/verify.sh --keep       leave Redis and the services running afterwards
#    scripts/verify.sh --no-docker  never touch Docker; expect Redis at $REDIS_URL
#
#  Expected last line: ALL CHECKS PASSED (exit code 0). Every step's full output is
#  kept in _verify_out/. docs/verification.md explains what each step proves.
#
#  Owns the whole lifecycle on purpose. It stops anything already listening on
#  8100-8103 first: a stale service process still running pre-edit code is the most
#  misleading failure there is -- you fix a bug, rerun, and watch the old process
#  fail in exactly the old way.
# ============================================================================

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

OUT="_verify_out"
KEEP=0
USE_DOCKER=1
for arg in "$@"; do
    case "$arg" in
        --keep) KEEP=1 ;;
        --no-docker) USE_DOCKER=0 ;;
        -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg (try --help)"; exit 2 ;;
    esac
done

FAILURES=0
STEP=0
STARTED_REDIS=0
SERVICES_PID=""
TORN_DOWN=0
export REDIS_URL="${REDIS_URL:-redis://localhost:6379/0}"
SERVICE_PORTS=(8100 8101 8102 8103)

step() { STEP=$((STEP + 1)); echo; echo "[$STEP] $*"; }
ok()   { echo "    ok    $*"; }
bad()  { echo "    FAIL  $*"; FAILURES=$((FAILURES + 1)); }
note() { echo "          $*"; }

echo
echo "==========================================================================="
echo "  Sluice end-to-end verification"
echo "==========================================================================="

# ------------------------------------------------------------------- preflight
# Prefer the repo's venv; fall back to whatever python3 is on PATH.
if [[ -n "${PYTHON:-}" ]]; then PY="$PYTHON"
elif [[ -x .venv/bin/python ]]; then PY=".venv/bin/python"
else PY="$(command -v python3 || true)"
fi
if [[ -z "$PY" ]]; then echo "  FATAL: no python3 found"; exit 1; fi

# Works whether or not the package is installed: src/ goes on the path.
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "  FATAL: $("$PY" --version 2>&1) is too old; Sluice needs Python 3.10+"
    exit 1
fi
if ! "$PY" -c 'import fastapi, uvicorn, httpx, redis, pytest' 2>/dev/null; then
    echo "  FATAL: dependencies missing for $PY. Create the environment first:"
    echo '      python3 -m venv .venv && source .venv/bin/activate'
    echo '      pip install -e ".[dev]"'
    exit 1
fi
echo "  python  $("$PY" --version 2>&1)  ($PY)"

redis_ping() {
    "$PY" - <<'EOF' 2>/dev/null
import os, sys, redis
url = os.environ["REDIS_URL"]
sys.exit(0 if redis.Redis.from_url(url, socket_connect_timeout=2).ping() else 1)
EOF
}

services_healthy() {
    "$PY" - <<'EOF' 2>/dev/null
import sys, httpx
ok = all(httpx.get(f"http://127.0.0.1:{p}/health", timeout=2).status_code == 200
         for p in (8100, 8101, 8102, 8103))
sys.exit(0 if ok else 1)
EOF
}

pids_on_port() {
    ss -ltnpH "( sport = :$1 )" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u
}

stop_services() {
    [[ -z "$SERVICES_PID" ]] && return 0
    local children
    children=$(pgrep -P "$SERVICES_PID" 2>/dev/null)
    # SIGTERM reaches the supervisor, which stops its four children itself.
    kill -TERM "$SERVICES_PID" 2>/dev/null
    for _ in $(seq 1 40); do kill -0 "$SERVICES_PID" 2>/dev/null || break; sleep 0.25; done
    # Belt and braces, in case anything outlived it.
    for pid in $children "$SERVICES_PID"; do kill -KILL "$pid" 2>/dev/null; done
    SERVICES_PID=""
    echo "  Effect services stopped."
}

teardown() {
    [[ "$TORN_DOWN" == 1 ]] && return
    TORN_DOWN=1
    echo
    if [[ "$KEEP" == 1 ]]; then
        echo "  Leaving Redis and the effect services running (--keep)."
        [[ -n "$SERVICES_PID" ]] && echo "  Stop the services with:  kill $SERVICES_PID"
        [[ "$STARTED_REDIS" == 1 ]] && echo "  Stop Redis with:         docker stop sluice-redis"
        return
    fi
    stop_services
    if [[ "$STARTED_REDIS" == 1 ]] && docker stop sluice-redis >/dev/null 2>&1; then
        echo "  Redis container stopped (this script started it)."
    fi
}
trap teardown EXIT
trap 'echo; echo "  interrupted"; exit 130' INT TERM

# ------------------------------------------------------------ 1. clean remnants
step "Removing remnants"
# A stale .sluice/shared.db makes a fresh run replay instead of execute -- and
# still print EEO PASS. This is the most likely way to fool yourself.
rm -rf .sluice .pytest_cache "$OUT"
find src tests -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null
mkdir -p "$OUT"
ok "removed .sluice, .pytest_cache, __pycache__, $OUT"
for port in "${SERVICE_PORTS[@]}"; do
    for pid in $(pids_on_port "$port"); do
        note "stopping stale listener on $port (pid $pid)"
        kill -TERM "$pid" 2>/dev/null
    done
done
sleep 0.5

# ----------------------------------------------------------------------- 2. redis
step "Redis ($REDIS_URL)"
if redis_ping; then
    ok "already up"
elif [[ "$USE_DOCKER" == 0 ]]; then
    bad "not reachable, and --no-docker was given"
else
    if ! docker info >/dev/null 2>&1; then
        note "docker daemon not reachable; trying to start it"
        sudo -n systemctl start docker 2>/dev/null || sudo -n service docker start 2>/dev/null
    fi
    if ! docker info >/dev/null 2>&1; then
        bad "docker daemon is not running. Start it (WSL: sudo service docker start, or"
        note "sudo systemctl start docker with systemd enabled), or use Docker Desktop's"
        note "WSL integration, then re-run. See docs/wsl-docker.md."
    else
        # docker-compose.yml pins container_name, so a container created by another
        # checkout belongs to a different compose project and `up` will refuse it.
        if docker compose up -d redis >"$OUT/redis_up.txt" 2>&1 \
           || docker start sluice-redis >>"$OUT/redis_up.txt" 2>&1; then
            STARTED_REDIS=1
        fi
        for _ in $(seq 1 30); do redis_ping && break; sleep 1; done
        if redis_ping; then ok "started sluice-redis"
        else bad "redis never came up; see $OUT/redis_up.txt"; fi
    fi
fi

# -------------------------------------------------------------------- 3. import
step "Package imports"
if VERSION=$("$PY" -c 'import sluice; print(sluice.__version__)' 2>"$OUT/import.txt"); then
    ok "sluice $VERSION"
else
    bad "package does not import; see $OUT/import.txt"
fi

# --------------------------------------------------------------------- 4. tests
step "Test suite: unit + integration (own topology on free ports, Redis DB 15)"
"$PY" -m pytest -q -rs >"$OUT/pytest.txt" 2>&1
PYTEST_RC=$?
SUMMARY=$(tail -n 1 "$OUT/pytest.txt")
if [[ $PYTEST_RC -ne 0 ]]; then
    bad "$SUMMARY  (see $OUT/pytest.txt)"
elif grep -q "skipped" <<<"$SUMMARY"; then
    # A skip means something was never exercised. Not a pass.
    bad "$SUMMARY -- something was skipped; see the reasons in $OUT/pytest.txt"
else
    ok "$SUMMARY"
fi

# ----------------------------------------------------------------- 5. services
step "Effect services (ledger 8100, ticket 8101, channel 8102, pager 8103)"
"$PY" -m sluice services >"$OUT/services.log" 2>&1 &
SERVICES_PID=$!
echo "$SERVICES_PID" >"$OUT/services.pid"
for _ in $(seq 1 40); do services_healthy && break; sleep 0.5; done
if services_healthy; then
    ok "all four healthy (supervisor pid $SERVICES_PID)"
else
    bad "services never became healthy; see $OUT/services.log"
fi

# ---------------------------------------------------------------------- 6. demo
step "Three-pane demo (expect 1/1/0, 2/2/1, 1/1/1, 0 unexplained)"
if "$PY" -m sluice demo --db-dir .sluice >"$OUT/demo.txt" 2>&1; then
    missing=0
    for want in "tickets 1   posts 1   pages 0" "tickets 2   posts 2   pages 1" \
                "tickets 1   posts 1   pages 1" "unexplained violations:    0"; do
        grep -qF "$want" "$OUT/demo.txt" || { note "missing: $want"; missing=1; }
    done
    if [[ $missing == 0 ]]; then ok "pinned 1/1/0, naive 2/2/1, sluice 1/1/1, 0 unexplained"
    else bad "scoreboard did not match; see $OUT/demo.txt"; fi
else
    bad "demo exited nonzero; see $OUT/demo.txt"
fi

# --------------------------------------------------------------------- 7. smoke
step "Topology smoke (real sockets + stream; expect 0 failed, 0 skipped)"
redis_ping || note "WARNING: Redis went away since step 2; stream checks will fail for that reason"
if "$PY" -m sluice smoke >"$OUT/smoke.txt" 2>&1; then
    if grep -q "\[ skip \]" "$OUT/smoke.txt"; then
        bad "something was skipped -- the stream was probably never exercised; see $OUT/smoke.txt"
    else
        ok "$(grep 'passed,' "$OUT/smoke.txt" | sed 's/^ *//')"
    fi
else
    bad "smoke failed:"
    grep "FAIL " "$OUT/smoke.txt" | sed 's/^/          /'
fi

# --------------------------------------------- 8. redis pipeline, fresh execution
WF=$("$PY" -c 'from sluice.core.tools import DEMO_ALERT
from sluice.core.types import workflow_id_for
print(workflow_id_for(DEMO_ALERT.alert_id))')

reset_all() {
    # Journal, idempotency keys and epochs, ledger, stream: all four, or the next run
    # replays instead of executing and proves nothing.
    rm -rf .sluice
    "$PY" - <<'EOF' >/dev/null 2>&1
import os, httpx, redis
from sluice.ingest import DEFAULT_STREAM
httpx.post("http://127.0.0.1:8100/reset", timeout=5)
for port in (8101, 8102, 8103):
    httpx.post(f"http://127.0.0.1:{port}/admin/reset", timeout=5)
redis.Redis.from_url(os.environ["REDIS_URL"]).delete(DEFAULT_STREAM)
EOF
}

counts() {
    WF="$WF" "$PY" - <<'EOF' 2>&1
import json, os, httpx
r = httpx.get("http://127.0.0.1:8100/counts",
              params={"net": "true", "workflow_id": os.environ["WF"]}, timeout=5)
print(json.dumps(r.json(), sort_keys=True))
EOF
}

orchestrate() {
    "$PY" -m sluice orchestrator --owner orch-a --source redis --world http \
        --once --db .sluice/shared.db "$@"
}

step "Redis -> orchestrator -> HTTP, fresh execution"
reset_all
# Configure the fault this step needs, rather than inherit whatever ran last.
"$PY" -m sluice faults poison >"$OUT/faults.txt" 2>&1 || bad "could not set faults; see $OUT/faults.txt"
"$PY" -m sluice producer --demo --count 1 >"$OUT/producer1.txt" 2>&1
orchestrate --narrate >"$OUT/orch_fresh.txt" 2>&1
if grep -q "\[ingest\] redis stream" "$OUT/orch_fresh.txt"; then
    ok "consumed from the Redis stream (not the in-process fallback)"
else
    bad "fell back to the in-process queue; Redis was NOT exercised"
fi
if grep -q "barrier_released" "$OUT/orch_fresh.txt"; then
    ok "barrier blocked, both effects compensated (LIFO), barrier released"
else
    bad "no barrier_released -- compensation never ran; see $OUT/orch_fresh.txt"
fi
if grep -q "EEO PASS" "$OUT/orch_fresh.txt"; then
    ok "$(grep 'a-1001:' "$OUT/orch_fresh.txt" | sed 's/^ *//')"
else
    bad "EEO did not pass; see $OUT/orch_fresh.txt"
fi

# ------------------------------------------------------------ 9. replay is a no-op
step "Same alert again, journal intact (expect replay, not re-execution)"
"$PY" -m sluice producer --demo --count 1 >"$OUT/producer2.txt" 2>&1
orchestrate --narrate >"$OUT/orch_replay.txt" 2>&1
REPLAYED=$(grep -c "step_replayed" "$OUT/orch_replay.txt")
if [[ "$REPLAYED" == 8 ]]; then
    ok "all 8 steps replayed from the journal, nothing re-executed"
else
    bad "expected 8 step_replayed, got $REPLAYED; see $OUT/orch_replay.txt"
fi

# ------------------------------------------- 10. exactly-once under redelivery
step "At-least-once redelivery must not duplicate effects"
counts >"$OUT/counts_before.txt"
"$PY" -m sluice producer --demo --count 1 --redeliver >"$OUT/producer3.txt" 2>&1
orchestrate >"$OUT/orch_dup.txt" 2>&1
counts >"$OUT/counts_after.txt"
if cmp -s "$OUT/counts_before.txt" "$OUT/counts_after.txt"; then
    ok "ledger counts for $WF unchanged across the duplicate:"
    note "$(cat "$OUT/counts_after.txt")"
else
    bad "ledger counts moved after a duplicate delivery"
    note "before: $(cat "$OUT/counts_before.txt")"
    note "after:  $(cat "$OUT/counts_after.txt")"
fi

# ------------------------------------------------------------------- teardown
teardown
echo
echo "==========================================================================="
if [[ $FAILURES -eq 0 ]]; then
    echo "  ALL CHECKS PASSED"
    echo "  Full output kept in $OUT/ if you want to read it."
    echo "==========================================================================="
    exit 0
fi
echo "  $FAILURES CHECK(S) FAILED -- see $OUT/ for the full output"
echo "==========================================================================="
exit 1
