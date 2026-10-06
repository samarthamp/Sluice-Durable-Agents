# Command reference

Everything runs through one command, `sluice <command>`. After `pip install -e .` it
is on your `PATH`; without installing, use `python -m sluice <command>` with
`PYTHONPATH=src`. Each subcommand is also a module (`python -m sluice.cli.demo`).

```
usage: sluice <command> [options]

Divergence-safe durable execution for agent decisioning.

commands:
  demo          three-pane scenarios, crash sweep, failover, overhead benchmark
  services      start the ledger and the ticket/channel/pager effect services
  orchestrator  run one orchestrator process against the shared journal
  producer      publish synthetic alerts to the Redis stream
  dashboard     serve the read-only dashboard
  smoke         layer-by-layer check of a running topology
  faults        show or set fault injection on the running services

Run 'sluice <command> --help' for that command's options.
```

`sluice --version` prints the version. The help text below is the CLI's own output.

- [demo](#sluice-demo) · [services](#sluice-services) ·
  [orchestrator](#sluice-orchestrator) · [producer](#sluice-producer) ·
  [dashboard](#sluice-dashboard) · [smoke](#sluice-smoke) · [faults](#sluice-faults)
- [Environment variables](#environment-variables) · [HTTP APIs](#http-apis) ·
  [Tuning knobs](#tuning-knobs) · [Files it writes](#files-it-writes)

---

## sluice demo

Runs a scenario through the three recovery strategies, in-process by default, and prints
each pane's outcome, scoreboard and EEO verdict. Also hosts the crash sweep, the failover
demo and the benchmark. Exit code 0 when every `sluice`-mode pane passes EEO.

```bash
sluice demo                                  # the poison step
sluice demo --scenario all --flush-late      # every scenario, late pages fired at once
sluice demo --slow 0.35                      # paced and narrated
sluice demo --modes sluice --verbose --audit-dir audit/
sluice demo --world http                     # against `sluice services`
sluice demo --sweep                          # 165 runs; --sweep-quick for 51
sluice demo --failover
sluice demo --bench --bench-concurrency 4
```

| Scenario | What it stresses |
|---|---|
| `poison` | wrong classification; the page to an empty rota fails |
| `residue` | the wrong page already rang before the mistake was found |
| `compfail` | compensation fails for good: the bounded barrier escalates |
| `compretry` | compensation fails once, then succeeds on retry |
| `zombie` | the page times out, is surfaced as unknown, then lands late |
| `crash` | a process crash at `--crash-seq` / `--crash-phase`, then recovery |
| `redelivery` | the same alert delivered twice |

```
usage: sluice demo [-h]
                   [--scenario {compfail,compretry,crash,poison,redelivery,residue,zombie,all}]
                   [--modes MODES] [--world {inprocess,http}] [--db-dir DB_DIR]
                   [--slow SECONDS] [--late-delay LATE_DELAY] [--flush-late]
                   [--crash-seq CRASH_SEQ]
                   [--crash-phase {before_intent,after_intent,after_effect,after_result}]
                   [--audit-dir AUDIT_DIR] [--sweep] [--sweep-quick] [--failover] [--bench]
                   [--bench-n BENCH_N] [--bench-concurrency BENCH_CONCURRENCY]
                   [--bench-effect-latency-ms BENCH_EFFECT_LATENCY_MS] [--narrate] [--verbose]
                   [--traceback]

Sluice demo

options:
  -h, --help            show this help message and exit
  --scenario {compfail,compretry,crash,poison,redelivery,residue,zombie,all}
                        which scenario to run across the panes (default: poison)
  --modes MODES         comma-separated subset of pinned,naive,sluice (default: all three)
  --world {inprocess,http}
                        http requires `sluice services` to be up
  --db-dir DB_DIR       where journals are written; point the dashboard here (default:
                        .sluice)
  --slow SECONDS        pause between steps so the room can watch (try 0.35)
  --late-delay LATE_DELAY
                        late-delivery window: 8 on stage, 40 in the sweep (3.3)
  --flush-late          fire late deliveries immediately instead of waiting
  --crash-seq CRASH_SEQ
                        step to crash at, 0-7, for --scenario crash (default: 4)
  --crash-phase {before_intent,after_intent,after_effect,after_result}
                        where in the step the crash lands (default: after_effect)
  --audit-dir AUDIT_DIR
                        write an incident post-mortem per pane
  --sweep               run the crash sweep (2.8)
  --sweep-quick         a short sweep
  --failover            lease and fencing demo (3.4)
  --bench               overhead benchmark: what durable execution costs (7.6)
  --bench-n BENCH_N     workflows per configuration (default: 200)
  --bench-concurrency BENCH_CONCURRENCY
                        also measure K threads sharing one journal (0 = skip)
  --bench-effect-latency-ms BENCH_EFFECT_LATENCY_MS
                        per-effect latency for the realistic-denominator rows (0 = skip those
                        rows)
  --narrate             stream events live; implied by --slow
  --verbose             also print each pane's branch tree, step by step
  --traceback           print the traceback when a pane halts
```

## sluice services

Starts the HTTP topology: the ground-truth ledger (8100), ticket (8101), channel (8102) and
pager (8103), each as its own OS process, supervised. Ctrl-C or `kill <pid>` stops all
four.

```bash
sluice services                                    # all four
sluice services --service pager --port 9103        # one, in the foreground
```

```
usage: sluice services [-h] [--service {ledger,ticket,channel,pager}] [--port PORT]
                       [--host HOST] [--ledger-url LEDGER_URL]

Sluice effect services

options:
  -h, --help            show this help message and exit
  --service {ledger,ticket,channel,pager}
                        run a single service in the foreground
  --port PORT           with --service: listen here (default: 8100-8103 by service)
  --host HOST           bind address (default: 127.0.0.1)
  --ledger-url LEDGER_URL
                        where effect services record ground truth (default: http://HOST:8100)
```

## sluice orchestrator

One orchestrator process. Consumes alerts, drives each workflow to a terminal state, grades
it against the ledger, writes the verdict beside the journal, then acknowledges the alert.
Run two with different `--owner` values against the same `--db` for a real leader race.

```bash
sluice orchestrator --once --narrate                         # drain the stream, then exit
sluice orchestrator --owner orch-a                           # long-running
sluice orchestrator --owner orch-b --claim-idle-ms 2000      # a standby
sluice orchestrator --world inprocess --source inprocess # no Docker, no services
```

The orchestrator does not set faults on the HTTP services. Set them first with
`sluice faults poison` to see the poison step over the real topology.

```
usage: sluice orchestrator [-h] [--owner OWNER] [--db DB] [--world {http,inprocess}]
                           [--source {redis,inprocess}] [--mode {sluice,pinned,naive}]
                           [--severity SEVERITY] [--lease-ttl LEASE_TTL]
                           [--max-attempts MAX_ATTEMPTS] [--claim-idle-ms CLAIM_IDLE_MS]
                           [--poll-s POLL_S] [--once] [--narrate] [--empty-rota EMPTY_ROTA]

Sluice orchestrator process

options:
  -h, --help            show this help message and exit
  --owner OWNER         lease owner id; make it unique (default: orch-a)
  --db DB               shared journal; both orchestrators must point at the same file
                        (default: .sluice/shared.db)
  --world {http,inprocess}
                        effect layer: the HTTP services, or in-process stubs (default: http)
  --source {redis,inprocess}
                        where alerts come from (default: redis)
  --mode {sluice,pinned,naive}
                        recovery strategy (default: sluice)
  --severity SEVERITY   the scripted agent's first classification (default: P2)
  --lease-ttl LEASE_TTL
                        seconds before a silent leader's lease can be taken (default: 30.0)
  --max-attempts MAX_ATTEMPTS
                        recovery attempts per alert before giving up (default: 4)
  --claim-idle-ms CLAIM_IDLE_MS
                        reclaim entries a dead consumer left pending for this long (default:
                        8000)
  --poll-s POLL_S       sleep between empty polls of the stream (default: 1.0)
  --once                drain what is queued, then exit
  --narrate             print every engine event as it happens
  --empty-rota EMPTY_ROTA
                        poison the demo rota (in-process world only; for the HTTP world use
                        `sluice faults poison`); '' to disable (default: rota-X)
```

## sluice producer

Publishes alerts to the Redis stream. `--demo` publishes the poison-step alert (`a-1001`);
otherwise alerts are random.

```bash
sluice producer --demo --count 1
sluice producer --demo --count 1 --redeliver   # at-least-once: the same alert twice
sluice producer --count 20 --rate 4
sluice producer --stats                        # depth, lag, pending entries
```

```
usage: sluice producer [-h] [--count COUNT] [--rate RATE] [--stream STREAM] [--group GROUP]
                       [--demo] [--redeliver] [--stats]

Sluice alert producer

options:
  -h, --help       show this help message and exit
  --count COUNT    how many alerts to publish (default: 10)
  --rate RATE      alerts per second (default: 2.0)
  --stream STREAM  Redis stream key (default: alerts:incoming)
  --group GROUP    consumer group to create if missing (default: orchestrators)
  --demo           publish the poison-step alert instead of random ones
  --redeliver      publish each alert twice, to exercise at-least-once
  --stats          print stream stats and exit
```

## sluice dashboard

Serves the read-only dashboard at `http://127.0.0.1:8000`. It reads the three demo
journals in `--db-dir` and, if present, the distributed run's journal (`--live-db`).

```bash
sluice dashboard                         # read-only
sluice dashboard --allow-control         # adds Run / Run distributed buttons
```

```
usage: sluice dashboard [-h] [--db-dir DB_DIR] [--host HOST] [--port PORT] [--allow-control]
                        [--live-db LIVE_DB]

Sluice dashboard

options:
  -h, --help         show this help message and exit
  --db-dir DB_DIR    where the demo journals live (default: .sluice)
  --host HOST        bind address (default: 127.0.0.1)
  --port PORT        port (default: 8000)
  --allow-control    mount POST /api/run so the page can launch scenarios itself
  --live-db LIVE_DB  journal written by `sluice orchestrator`; shown as the live distributed
                     pane. '' to hide it
```

## sluice smoke

Checks a running topology layer by layer: dependencies, the services (four distinct
processes, idempotency across a socket, compensation, probing, fencing, a real read timeout,
a refused connection), the Redis stream (consumer group, pending entries, `XAUTOCLAIM`),
and one end-to-end poison step graded against the ledger. Expected: `20 passed, 0 failed,
0 skipped`. A `skip` means something was never exercised; treat it as a failure.

```
usage: sluice smoke [-h] [--http] [--redis] [--skip-deps]

Sluice topology smoke test

options:
  -h, --help   show this help message and exit
  --http       only the effect services
  --redis      only the stream
  --skip-deps  skip the installed-packages check
```

## sluice faults

Shows or sets fault injection on the running effect services. The services keep their
faults until told otherwise, and `/admin/reset` does not clear them.

```bash
sluice faults                        # what each service is injecting now
sluice faults poison                 # rota-X has nobody on call
sluice faults zombie                 # the pager times out, then the page lands late
sluice faults clear
```

```
usage: sluice faults [-h] [--service {ticket,channel,pager}] [{clear,poison,zombie}]

Show or set fault injection on the running effect services

positional arguments:
  {clear,poison,zombie}
                        apply this preset; omit it to show the current faults

options:
  -h, --help            show this help message and exit
  --service {ticket,channel,pager}
                        only this service (repeatable); default: all three
```

For anything beyond the presets, post the fields directly:

```bash
curl -X POST localhost:8103/admin/faults -H 'content-type: application/json' \
     -d '{"latency_s": 3.0}'                 # beyond the 2 s client timeout: a real unknown
curl -X POST localhost:8101/admin/faults -H 'content-type: application/json' \
     -d '{"down_services": ["ticket"]}'      # compensation can no longer close tickets
```

| Field | Type | Effect |
|---|---|---|
| `latency_s`, `jitter_s` | float | delay before every effect |
| `fail_tools` | list of tools | the effect is rejected (`failed`) |
| `timeout_tools` | list of tools | the effect times out (`unknown`) |
| `late_delivery_tools` | list of tools | a timed-out effect lands anyway, after `late_delivery_delay_s` |
| `down_services` | list of services or tools | refused immediately (`failed`), compensation too |
| `fail_compensation_tools` | list of tools | compensation is rejected |
| `empty_rotas` | list of rotas | a page to that rota fails: nobody on call |

---

## Environment variables

| Variable | Default | Used by |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | producer, orchestrator, smoke |
| `SLUICE_REDIS` | unset | library default for `sluice.alert_source()` when it is called without `use_redis` (the CLI always passes `--source` explicitly) |
| `SLUICE_LEDGER_URL` | `http://127.0.0.1:8100` | every HTTP client of the ledger |
| `SLUICE_TICKET_URL` | `http://127.0.0.1:8101` | `HttpWorld` |
| `SLUICE_CHANNEL_URL` | `http://127.0.0.1:8102` | `HttpWorld` |
| `SLUICE_PAGER_URL` | `http://127.0.0.1:8103` | `HttpWorld` |
| `SLUICE_TEST_REDIS_URL` | DB 15 on `REDIS_URL`'s server | the test suite (refuses DB 0) |
| `PYTHON` | `.venv/bin/python`, else `python3` | `scripts/verify.sh` |

## HTTP APIs

**Effect services** (ticket 8101, channel 8102, pager 8103):

| Endpoint | Purpose |
|---|---|
| `POST /execute` | `{tool, args, key, epoch, workflow_id, timeout_s}` → `{status, value, error}`; 409 on a stale epoch, 404 for a tool the service does not own |
| `POST /compensate` | same body; undoes the effect under `key` |
| `GET /probe?tool=&key=` | `done` / `not_done` / `unknown` (the pager always says `unknown`) |
| `GET /health` | pid, tools, faults, highest epoch per workflow, committed keys, ledger buffer |
| `GET` / `POST /admin/faults` | read or set fault injection |
| `POST /admin/reset` | forget idempotency keys and epochs (faults are kept) |
| `POST /admin/flush_late` | fire pending late deliveries now |

**Ledger** (8100): `POST /record`, `GET /effects?tool=&workflow_id=`,
`GET /compensations?workflow_id=`, `GET /counts?net=true|false&workflow_id=`,
`GET /health`, `POST /reset`.

**Dashboard** (8000): `GET /` (the page), `GET /api/state` (every pane, derived from the
journals), `GET /api/audit?mode=` (Markdown post-mortem), `GET /api/health`. With
`--allow-control`: `POST /api/run?scenario=` (all three panes, in-process) and
`POST /api/run_live?scenario=&mode=` (producer → Redis → orchestrator → HTTP, as
subprocesses; `poison`, `zombie` and `redelivery` only).

## Tuning knobs

Engine bounds (`src/sluice/core/engine.py`, each also a keyword argument):

| Knob | Default | Meaning |
|---|---|---|
| `MAX_FORK_DEPTH` | 3 | forks before divergence escalates |
| `MAX_COMP_ATTEMPTS` | 3 | compensation rounds before the barrier escalates |
| `BARRIER_DEADLINE_S` | 5.0 | time budget for those rounds |
| `COMP_BACKOFF_S` | 0.05 | first backoff between rounds, doubling |
| `TIMEOUT_S` | 2.0 | client timeout per effect call |
| `LEASE_TTL_S` | 30.0 | lease duration |

Journal durability: `Journal(path, synchronous="FULL")` (default), `"NORMAL"` or `"OFF"`.
`sluice demo --bench` measures the difference.

## Files it writes

| Path | Written by | Contents |
|---|---|---|
| `.sluice/demo-{pinned,naive,sluice}.db` | `sluice demo` | one journal per pane (SQLite WAL) |
| `.sluice/shared.db` | `sluice orchestrator` | the journal both orchestrators share |
| `*.db.verdict.json` | demo, orchestrator | the EEO verdict, for the dashboard |
| `audit/*.md` | `sluice demo --audit-dir` | incident post-mortems |
| `_verify_out/` | `scripts/verify.sh`, `verify.bat` | every step's full output |

All of these are git-ignored. Delete `.sluice/` to make the next run execute rather
than replay.
