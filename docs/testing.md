# Testing, and finding the cause when something breaks

Two kinds of evidence, and they are not equivalent:

- **The EEO checker against the ground-truth ledger** is the oracle. It knows what
  happened to the world independently of what the system believed (see
  [architecture.md](architecture.md#verification-the-oracle)).
- **The test suite** is the safety net: 351 tests that pin each module's behaviour, cross
  every real boundary, and turn a regression into a red line that names the rule it broke.

## Running the tests

```bash
pytest                          # everything: 351 tests, ~70-90 s
pytest tests/unit               # one module at a time: a few seconds, no network
pytest tests/integration        # scenarios, HTTP processes, Redis, failover
pytest -m "not redis"           # skip what needs Redis
pytest -m "not slow"            # skip the multi-process pipeline tests
pytest -k barrier -v            # anything with "barrier" in its name
pytest -rs                      # show why anything was skipped
ruff check src tests            # lint (pyflakes, pycodestyle, isort, bugbear, pyupgrade)
```

The suite runs straight from a checkout: `pyproject.toml` puts `src/` on the path, so
`pip install -e .` is optional. Tests that need something missing skip with the reason:

| Needs | Marker | Without it |
|---|---|---|
| FastAPI, uvicorn, httpx | `http` | skipped: `pip install -e ".[http]"` |
| a reachable Redis | `redis` | skipped: `docker compose up -d` |
| both, plus several processes | `slow` | skipped if either is missing |

Integration tests never touch your demo setup:

- **Services** are launched as real processes on **free ports** (never 8100-8103), exactly
  as production launches them (`python -m sluice services --service NAME`), once per
  session, and reset before every test.
- **Redis** tests use **DB 15** on the server `REDIS_URL` names (override with
  `SLUICE_TEST_REDIS_URL`), flushed before and after. The fixture refuses DB 0.
- One test needs ports 8100-8103 free (it starts the real supervisor and SIGTERMs it); it
  skips if they are in use, and on non-Linux systems.

Two settings make failures loud rather than quiet: unknown markers are errors
(`--strict-markers`), and **an exception in any background thread fails the test** that
caused it. That second one is how a race in the shared journal was found: the old
benchmark's worker threads were dying silently.

## What each file covers

### `tests/unit/`: one module, synthetic inputs

| File | Pins down |
|---|---|
| `test_types.py` | serialisation round trips; effect key format and stability; workflow id derivation; **`InProcessWorld` and `HttpWorld` keep the `World` protocol's exact signatures** |
| `test_tools.py` | the eight tools, their effect types and owners, the scripted trace, ranked alternatives, the supersede annotation |
| `test_journal.py` | records, the branch tree, replay aids, the barrier predicates (`uncompensated`, `residue`), leases and epochs across connections and threads, read-only mode, WAL, durability across a reopen, **thread safety of a shared journal** |
| `test_world.py` | idempotency, per-workflow fencing, every fault mode, late delivery, compensation, probing, the ledger's net and gross counts, fault presets |
| `test_engine.py` | every recovery path: the poison step, pinned livelock, naive duplication, fork bound, alternatives exhausted, compensation retries, deadline and escalation, residue and supersede, probing, bounded ambiguity, late-delivery suppression, reconciliation, crash at all four boundaries, leases |
| `test_checker.py` | **each EEO clause triggered in isolation**: duplicate keys, an unexplained second page, a supersede pointing at nothing, silent loss, stranded effects (explained vs unexplained), compensation on a live branch, forward-order compensation, two unknowns at once; evidence table; verdict sidecar |
| `test_observability.py` | the gate's three still frames, the scoreboard, phones, branch views, the terminal fallback, every post-mortem section |
| `test_ingest.py` | the in-process source, `drain()` acking only after the handler returns, the loud fallback when Redis is down |
| `test_verification.py` | a slice of the crash sweep (25 runs, every fault mode) with zero unexplained violations; the benchmark's rows, and that no worker thread is lost |
| `test_cli.py` | the dispatcher, every subcommand's `--help`, `python -m sluice` with no install, the demo, the orchestrator draining a queue and restoring its signal handlers, clean failures when services or Redis are absent |
| `test_architecture.py` | **layers import only downwards** (AST scan, lazy imports included); the kernel stays stdlib-only; no references to pre-refactor entry points; `requirements.txt` matches `pyproject.toml`; the dashboard page ships in the package |

### `tests/integration/`: across boundaries

| File | Pins down |
|---|---|
| `test_invariants.py` | the scenario-level invariants: barrier scope (the "aunt" case), LIFO order, gate records, fork bound, bounded barrier both ways, residue, crash at a boundary, redelivery, zombie page, failover fencing, escalation rate as a derived number |
| `test_scenario_matrix.py` | **all 7 scenarios × 3 strategies**, with outcome, scoreboard and verdict asserted, and the journal's scoreboard cross-checked against the ledger |
| `test_services_api.py` | the effect and ledger services through their HTTP API: 409 fencing, 404 wrong tool, 422 validation, faults, reset semantics, the service ignoring the caller's deadline, ledger de-duplication, the buffering ledger client |
| `test_dashboard_api.py` | the read path from real journals, the verdict sidecar, the audit export, **reads never write**, control endpoints absent without the flag, refusals for what the distributed path cannot express |
| `test_http_topology.py` | four real service processes: real sockets, real read timeouts (`unknown`, then the page lands), refused connections (`failed`), fencing over the wire, **`HttpWorld` reproducing the in-process result for every strategy**, `smoke` and `faults` against live services, the supervisor stopping its children on SIGTERM |
| `test_redis_ingest.py` | consumer groups, pending entries, a survivor reclaiming a dead consumer's alert, lag and depth, redelivery collapsing into one workflow |
| `test_distributed_pipeline.py` | producer → Redis → orchestrator **process** → HTTP → ledger: fresh run, replay (8 × `step_replayed`), redelivery (counts unchanged); **`kill -9` the leader mid-compensation and let a standby finish**; the dashboard's distributed Run button |

### Regression tests for what this refactor found

Each was confirmed to fail on the pre-refactor code before the fix:

| Issue | Test | On the old code |
|---|---|---|
| `kill` orphaned the four services | `test_the_supervisor_stops_its_children_on_sigterm` | supervisor exits 143, all 4 children keep running |
| threads sharing one journal raced | `test_one_journal_is_safe_to_share_between_threads` | 4 to 6 of 6 threads fail per run (`InterfaceError`, wrong rows) |
| smoke left rota-X poisoned | `test_smoke_passes_and_leaves_no_fault_behind` | the pager keeps `empty_rotas=['rota-X']` |

## Root-cause playbook

### Start from the verdict

Every scenario prints its verdict. A failing one lists each violation with its clause:

```
  EEO:           FAIL
                 [UNEXPLAINED] clean_abandonment: 2 uncompensated on abandoned branch: [...]
```

| Clause | Usually means | Look at |
|---|---|---|
| `no_duplication` | the same key committed twice, or a second page without a supersede annotation | the ledger's effects for the workflow; the page step's `args` |
| `no_loss` | the workflow ended without a page or an escalation (pinned livelock is this) | the outcome and the last records of the active branch |
| `clean_abandonment` | an abandoned branch still holds effects, or compensation ran forwards or on a live branch | `COMP_RESULT` records and their `attempt` numbers |
| `bounded_ambiguity` | two unknown pages at once, or one never surfaced | `RESULT` records with status `unknown`, then `ESCALATED` |

### Read the branch tree

The post-mortem is the fastest way to see what happened, and in what order:

```bash
sluice demo --scenario compfail --modes sluice --verbose --audit-dir audit/
less audit/compfail-sluice.md      # branch tree, compensation table, full journal
```

Or straight from any journal, including `.sluice/shared.db` after a distributed run:

```bash
PYTHONPATH=src python3 - <<'EOF'
from sluice import Journal, derive_state, post_mortem, render_terminal
j = Journal(".sluice/shared.db", read_only=True)
wf = j.latest_workflow()
print(render_terminal(derive_state(j, wf)))    # compact: gate, scoreboard, one line per branch
print(post_mortem(j, wf))                      # everything
EOF
```

`render_terminal` draws each branch as a step string: `#` ok, `x` failed, `?` unknown,
`~` compensated, `>` in flight, `.` not reached.

### Reproduce a sweep failure

`sluice demo --sweep` names each failing run's fault mode and crash point:

```
1 run(s) with unexplained violations:
  [partition-transient  seq4:after_effect       ] -> completed
      clean_abandonment: ...
```

Re-run just that cell, and keep the journal to inspect:

```bash
PYTHONPATH=src python3 - <<'EOF'
from sluice.verification.sweep import sweep, format_failures
r = sweep(steps=[4], phases=("after_effect",), fault_modes=["partition-transient"],
          workdir="sweep-debug")
print(r["table"]); print(format_failures(r))
EOF
ls sweep-debug/                                 # one journal per run
```

`sluice demo --scenario crash --crash-seq 4 --crash-phase after_effect` reproduces a
crash point on its own, through all three strategies.

### Distributed runs

| Symptom | Meaning | Check |
|---|---|---|
| `[ingest] redis unavailable ... falling back` | Redis was not reachable; the run tested nothing about the stream | `docker ps`, `REDIS_URL` |
| `step_replayed` when you expected execution | a stale journal; the workflow already completed | delete `.sluice/` |
| no `barrier_blocked` on the poison step | the poison fault is not set on the services | `sluice faults`, then `sluice faults poison` |
| `services down: [...]` | `sluice services` is not running, or a stale process holds a port | `ss -ltnp \| grep 810` |
| `HTTP 409 ... fenced: stale epoch` | a lower epoch than the service has seen for that workflow: a deposed leader, or a journal deleted while the services kept their epochs | `POST /admin/reset` on the services, with the journal |
| `HTTP 422` on `/execute` | the request body failed validation | the note at the top of `world/services.py` |
| ledger counts moved after a redelivery | a real exactly-once violation | compare `/counts?workflow_id=...` before and after |
| `not leader` | another orchestrator holds the workflow's lease | wait for `--lease-ttl`, or `lease_info()` in the journal |

Service state at a glance: `curl localhost:8103/health` shows the pid, active faults,
highest epoch per workflow, committed keys, and how many ledger writes are buffered.
`scripts/verify.sh` keeps every step's full output in `_verify_out/`.
