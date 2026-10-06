# Verifying Sluice end to end

One command, on Linux or inside WSL2:

```bash
scripts/verify.sh               # clean, start Redis + services, verify, tear down
scripts/verify.sh --keep        # leave Redis and the services running afterwards
scripts/verify.sh --no-docker   # never touch Docker; expect Redis already at $REDIS_URL
```

Expected last line: **`ALL CHECKS PASSED`**, exit code 0. Anything else: read
`_verify_out/`, which keeps the full output of every step. A run takes about 90 seconds,
most of it the test suite.

`--keep` is the pre-demo mode: it verifies everything, then hands you a live topology to
point the dashboard at. If the script started Redis, it stops it again at the end (unless
`--keep`), so Docker is left as it was found.

On a Windows host with Redis in WSL2, use `scripts\verify.bat` instead (see
[the Windows section](#windows-host-scriptsverifybat)).

This page explains what each step does, **why it is there**, and what its output should
look like, so you can run any step by hand and know whether the answer is right. A green
run that never touched Redis is the failure mode this is built to make visible.

---

## The topology being verified

```
sluice producer  ──▶  Redis stream (alerts:incoming)        [Docker]
                                  │
                                  ▼
                       sluice orchestrator                  [journal: .sluice/shared.db]
                                  │  HTTP: key + epoch
                  ┌───────────────┼───────────────┐
                  ▼               ▼               ▼
              ticket:8101    channel:8102     pager:8103
                  └───────────────┼───────────────┘
                                  ▼
                            ledger:8100        ← the out-of-process oracle
```

The ledger is the point. It is a separate process that records ground truth, so
correctness is checked against something the orchestrator cannot lie to.

## Prerequisites

| Thing | Check | If missing |
|---|---|---|
| Python 3.10+ venv | `.venv/bin/python --version` | `python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"` |
| Docker | `docker info` | WSL: `sudo service docker start`; see [wsl-docker.md](wsl-docker.md) |
| Ports | `ss -ltnp \| grep -E ':(6379\|810[0-3])'` | the script stops stale listeners on 8100-8103 itself |

`verify.sh` uses `.venv/bin/python` if it exists, otherwise `python3`, or whatever `PYTHON`
names. It puts `src/` on `PYTHONPATH`, so it works with or without `pip install -e .`.

---

## Step 1: remove remnants

```bash
rm -rf .sluice .pytest_cache _verify_out
```

**Why:** `.sluice/shared.db` is the journal. A workflow already committed there will
**replay** instead of executing, so a stale journal turns a real test into a no-op that
still prints `EEO PASS`. This is the most likely way to fool yourself.

The script also stops anything listening on 8100-8103. A stale service process running
pre-edit code is the second most likely way: you fix a bug, rerun, and watch the old
process fail in exactly the old way.

## Step 2: Redis

```bash
docker compose up -d        # from the repo root
python -c "import redis; print(redis.Redis().ping())"
```

Expected: `True`. If the container exists but compose refuses it (`container name
"/sluice-redis" is already in use`), it was created from a checkout in another
directory: `docker start sluice-redis` (the script falls back to this itself). See
[wsl-docker.md](wsl-docker.md).

## Step 3: package imports

```bash
python -c "import sluice; print(sluice.__version__)"
```

Expected: `1.0.0`.

## Step 4: the test suite

```bash
pytest -q -rs
```

Expected: `351 passed`, **nothing skipped**. The integration tests start their own service
processes on free ports and use Redis DB 15, so they run before the demo topology exists
and never touch it. A skip means something was never exercised (usually Redis), so the
script counts it as a failure. See [testing.md](testing.md) for what each file proves.

## Step 5: effect services

```bash
sluice services
```

Expected: four processes, each with its own pid:

```
  ledger   pid 89355  http://127.0.0.1:8100
  ticket   pid 89359  http://127.0.0.1:8101
  channel  pid 89360  http://127.0.0.1:8102
  pager    pid 89363  http://127.0.0.1:8103

  all four up. ctrl-c to stop, or kill one pid to break a service.
```

**Why separate processes:** distinct pids are what make this a distributed system rather
than one interpreter pretending. `sluice smoke` asserts four distinct pids. `kill
<pid>` on the supervisor stops all four.

## Step 6: the three-pane demo

```bash
sluice demo
```

Expected scoreboards:

| Mode | tickets / posts / pages | Outcome | EEO |
|---|---|---|---|
| pinned | **1 / 1 / 0** | livelocked, nobody paged | FAIL: `no_loss` |
| naive | **2 / 2 / 1** | completed | FAIL: `clean_abandonment` |
| sluice | **1 / 1 / 1** | completed, rota-Y paged | **PASS** |

and at the bottom `unexplained violations:    0`.

**The two FAILs are the point.** They are the baselines the system is compared against:
pinned livelocks and loses the alert; naive acts twice and leaves two uncompensated
effects. Only Sluice gets 1/1/1 with a clean verdict.

## Step 7: topology smoke

```bash
sluice smoke
```

Expected: `20 passed, 0 failed, 0 skipped`. Covers HTTP round trips, idempotency across a
real socket, probe observability, fencing enforced by the service (HTTP 409), a genuine
read timeout yielding `unknown`, a refused connection yielding `failed`, five Redis stream
checks, and one end-to-end poison step graded against the ledger.

> **A `skip` is a failure here.** `skip redis reachable` means the stream was never
> exercised and five checks proved nothing.

Useful subsets: `sluice smoke --http`, `sluice smoke --redis`.

## Step 8: Redis → orchestrator → HTTP, fresh execution

```bash
sluice faults poison
sluice producer --demo --count 1
sluice orchestrator --owner orch-a --source redis --world http --once --narrate
```

**Set the fault explicitly.** The services keep their faults across `/admin/reset`, so
without this step the run inherits whatever ran last. (The old `verify.bat` only saw the
barrier here because the smoke test happened to leave rota-X poisoned.)

**The first line of the output is the one that matters:**

```
[ingest] redis stream alerts:incoming group orchestrators
```

If you see `[ingest] redis unavailable (...); falling back to in-process queue` instead,
nothing below it tested the stream. The fallback is deliberate, so a Docker problem cannot
kill a live demo, but it means a green run proves nothing about Redis.

Expected narration (the interesting part):

```
    [orch-a] branch_abandoned
    [orch-a] forked no responder on rota-X
    ...
    [orch-a] barrier_blocked 2 uncompensated effects on abandoned branch
    [orch-a] compensated post_to_channel
    [orch-a] compensated create_ticket
    [orch-a] barrier_released page_oncall
    [orch-a] intent page_oncall
    [orch-a] result page_oncall
    [orch-a] intent update_status_page
    [orch-a] result update_status_page
  a-1001: completed   rota=rota-Y       146ms  EEO PASS
```

Read it as: the first branch paged a rota with nobody on it, so the workflow forked; the
barrier refused to let the new branch page anyone until the abandoned branch's two effects
were cleaned up; both were compensated, newest first; the barrier lifted; rota-Y was paged
once. About 100-200 ms: real work across four processes.

## Step 9: replay is a no-op

Run the same two commands again without clearing anything:

```bash
sluice producer --demo --count 1
sluice orchestrator --owner orch-a --source redis --world http --once --narrate
```

Expected: all eight steps replayed, nothing re-executed:

```
    [orch-a] step_replayed fetch_alerts
    [orch-a] step_replayed fetch_service_context
    ...
  a-1001: completed   rota=rota-Y         6ms  EEO PASS
```

**Why:** the workflow id is derived from the alert id, so a redelivered `a-1001` lands on
the same workflow, and the journal already has every step committed. That is the
crash-recovery guarantee in one line: a recovering orchestrator resumes rather than redoes.

## Step 10: exactly-once under redelivery

```bash
WF=$(python -c "from sluice import DEMO_ALERT, workflow_id_for; print(workflow_id_for(DEMO_ALERT.alert_id))")
curl -s "http://127.0.0.1:8100/counts?net=true&workflow_id=$WF"
sluice producer --demo --count 1 --redeliver
sluice orchestrator --owner orch-a --source redis --world http --once
curl -s "http://127.0.0.1:8100/counts?net=true&workflow_id=$WF"
```

Expected: **identical before and after**:

```json
{"create_ticket": 1, "page_oncall": 1, "post_to_channel": 1, "update_status_page": 1, "write_dedupe_marker": 2}
```

**Why this is the headline result:** `--redeliver` publishes the same alert twice, on top
of the ones already delivered. Redis Streams are at-least-once, so all of them arrive. The
out-of-process ledger still shows exactly one ticket, one post, one page.

`write_dedupe_marker: 2` is correct: one per branch (original and forked). It is an
idempotent marker, not an outside-world effect, so it is not compensated.

**Compare per workflow, not globally.** The smoke test's deliberate read timeout leaves a
page in flight that can land a moment later under a different workflow; global counts can
move for reasons that have nothing to do with redelivery.

---

## Resetting between manual runs

To make step 8 execute rather than replay, clear all four: journal, service idempotency
keys, ledger, and stream:

```bash
rm -rf .sluice
for p in 8101 8102 8103; do curl -s -X POST localhost:$p/admin/reset >/dev/null; done
curl -s -X POST localhost:8100/reset >/dev/null
python -c "import redis; from sluice.ingest import DEFAULT_STREAM; redis.Redis().delete(DEFAULT_STREAM)"
sluice faults poison
```

Clearing only the journal still gives you the narration, but the ledger counts will not
move: the services deduplicate on the effect key independently, so a replayed effect
returns its cached result and never reaches the ledger again.

## Reading failures

| Symptom | Meaning |
|---|---|
| `[ingest] redis unavailable ... falling back` | Redis is down; the run tested nothing about the stream |
| `skip` in pytest or smoke | something was never exercised; usually Redis |
| `step_replayed` when you expected execution | stale `.sluice/shared.db`; clear it |
| no `barrier_blocked` in step 8 | the poison fault is not set: `sluice faults` to check |
| `HTTP 422 ... "loc":["query","body"]` | a FastAPI model was not resolved; see the note atop `src/sluice/world/services.py` |
| `services down: [...]` | `sluice services` is not running, or a stale process holds the port |
| `XAUTOCLAIM returned nothing`, then steps 8-10 fail | Redis died mid-run: check `docker ps -a` (see [wsl-docker.md](wsl-docker.md)) |
| ledger counts moved in step 10 | a genuine exactly-once violation. This is the one that matters |

More in the [root-cause playbook](testing.md#root-cause-playbook).

---

## Windows host: `scripts\verify.bat`

For running from Windows with Redis in WSL2's Docker Engine. Same ten steps, plus the WSL
handling a Windows-hosted run needs:

- It needs a **Windows** venv at `.venv\Scripts\python.exe` (`pip install -e ".[dev]"`).
- It starts Redis inside the `Ubuntu-24.04` distro (`systemctl start docker`, then `docker
  compose up -d`, with the repo path translated by `wslpath`). Edit `WSL_DISTRO` at the
  top if your distro is named differently.
- **WSL tears the distro down once the last `wsl.exe` client disconnects**, and that
  SIGTERMs the Redis container with it (observed dying 16 s after `docker compose up`).
  The script holds one hidden `wsl.exe` client open for the run and drops it at teardown,
  or keeps it with `--keep`.
- It uses `pushd`, not `cd /d`, so it also works when the repo lives on the WSL filesystem
  (`\\wsl.localhost\...`), which `cmd.exe` cannot `cd` into.
- One test (SIGTERM to the services supervisor) is POSIX-only and skips on Windows.

With `--keep`, pids are left in `_verify_out\services.pid` and `_verify_out\wsl.pid`;
`taskkill /PID <pid> /T /F` on each stops them.

---

## The two-orchestrator leader race

Automated in `tests/integration/test_distributed_pipeline.py`: it starts a leader, waits
until it is in the middle of compensation, `kill -9`s it, lets the lease expire, and checks
that a standby reclaims the alert, resumes from the journal at a higher epoch, and finishes
with exactly one page to the right rota.

By hand, with the services and Redis up. A workflow normally finishes in ~150 ms, too fast
to kill by hand, so slow every effect down to one second first (below the 2 s client
timeout, so nothing turns `unknown`):

```bash
sluice faults poison
for p in 8101 8102 8103; do
  curl -s -X POST localhost:$p/admin/faults -H 'content-type: application/json' \
       -d '{"latency_s": 1.0}' >/dev/null
done
sluice orchestrator --owner orch-a --lease-ttl 5 --narrate              # terminal 1
sluice orchestrator --owner orch-b --claim-idle-ms 2000 --narrate       # terminal 2
sluice producer --demo --count 1                                        # terminal 3
```

`kill -9` whichever one picked the alert up, mid-workflow. The survivor reclaims the
pending alert and takes the lease once it expires; the epoch increments. If the dead leader came back,
the services would refuse its stale epoch over a real socket, rather than trust it to
stand down.
