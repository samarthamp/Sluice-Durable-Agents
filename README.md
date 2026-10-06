# Sluice

**Divergence-safe durable execution for AI agents that act.**

![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-HTTP%20services-009688?logo=fastapi&logoColor=white)
![Redis](https://img.shields.io/badge/Redis-Streams-DC382D?logo=redis&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-WAL%20journal-003B57?logo=sqlite&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)
![pytest](https://img.shields.io/badge/tests-351%20passing-2E7D32?logo=pytest&logoColor=white)

When an automated decisioning layer crashes mid-decision, it either loses the signal or
duplicates it. Durable-execution engines (Temporal, Restate, DBOS) recover by *replaying*
journaled decisions, which assumes the decisions were right and deterministic. An AI agent's
are neither. Sluice is a recovery engine for agents whose actions have side effects in
the real world: it forks the journal at a bad decision, cleans up what the abandoned path
already did, and only then takes the irreversible action.

> A sluice is a gate that holds the flow back until the channel behind it is drained, then
> lifts. Here the gate stands in front of every irreversible action: nothing goes through
> until the path the agent abandoned has been cleaned up, newest effect first. That path is
> never erased, either: the journal is a tree, so it stays on record, which is what makes
> the cleanup provable and the audit trail real.

Built for the Signal Labs AI HackDay (Hyderabad), Distributed Systems track, as the
reliability layer an automated-decisioning product like Signal Labs' SignalOS needs once it
stops recommending and starts acting. The demo domain is incident triage: an agent reads an
alert, classifies it, then opens a ticket, posts to a channel and pages the on-call.

---

## Contents

- [The problem](#the-problem) · [How it works](#how-it-works) · [Architecture](#architecture)
- [Features](#key-features) · [Tech stack](#tech-stack) · [Project structure](#project-structure)
- [Quick start](#quick-start) · [Running it distributed](#running-it-as-a-distributed-system)
- [Testing and verification](#testing-and-verification) · [Results](#results)
- [Where it falls over](#where-it-falls-over) · [Documentation](#documentation)

---

## The problem

An agent classifies an incident as **P2**, which routes to **rota-X**, and starts acting:
dedupe marker, ticket, channel post, page. The page fails because rota-X has nobody on
call. The right answer was P1, and rota-Y. Recovery now has two conventional options, and
both are wrong:

| Strategy | What happens | Outcome |
|---|---|---|
| **Pinned replay**: what durable execution guarantees | replays the journaled P2, which fails again, forever | 1 ticket · 1 post · **0 pages**. The signal is lost |
| **Naive re-run**: start the agent over | reaches P1, but the first attempt's effects are still standing | **2 tickets · 2 posts** · 1 page. Duplicates, no cleanup |
| **Sluice** | forks at the decision, compensates the abandoned branch, then pages | 1 ticket · 1 post · **1 page, to the right rota** |

## How it works

![Recovering the poison step](docs/images/recovery-protocol.drawio.svg)

Three ideas carry the design:

1. **The journal is a tree, not a log.** Recovery that wants a different path forks a
   branch and marks the old one abandoned. The journal records what was tried, not just
   what was done.
2. **Tool calls are typed by effect.** `pure` / `idempotent` / `compensatable` /
   `irreversible`, crossed with `observable` / `unobservable`. That makes recovery
   decidable: probe what can be observed, undo what can be compensated, escalate what can
   be neither.
3. **The irreversible barrier.** A branch may not execute an irreversible effect while
   *any* abandoned branch in the workflow holds uncompensated effects. Compensation runs in
   reverse execution order (LIFO, as in sagas). The barrier is **bounded**: if cleanup
   keeps failing it retries to a deadline, then escalates to a human with the branch tree
   attached. It never hangs and never acts blind.

Correctness is a checkable property, **Effect-Exactly-Once** (EEO). After crash, recovery
and quiescence: (1) no irreversible effect commits twice per decision point, (2) every
workflow ends in a committed action or a surfaced escalation, (3) every effect on an
abandoned branch is compensated, in reverse order. Exactly-once is impossible for an
irreversible *and* unobservable effect (the two generals problem), so that case is
confined to one unknown at a time and always surfaced. EEO is graded against an
out-of-process **ground-truth ledger**, never against what the system believed.

## Architecture

![System topology](docs/images/architecture.drawio.svg)

| Port | Process | Role |
|---|---|---|
| 6379 | Redis (Docker) | alert stream, consumer group, at-least-once delivery |
| n/a | `sluice orchestrator` (x2) | leader + standby, sharing one SQLite journal and a lease |
| 8100 | ledger | ground truth: what actually happened. **The oracle** |
| 8101 | ticket | compensatable, observable |
| 8102 | channel | compensatable, externally visible |
| 8103 | pager | **irreversible and unobservable**: no undo, no status query |
| 8000 | dashboard | read-only; reads the journal, never drives execution |

The effect services are separate OS processes behind real sockets, so crashes, timeouts and
partitions are real events. A read timeout is `unknown`, not `failed`: slow and crashed are
indistinguishable. Two orchestrators race for a per-workflow lease with a monotonically
increasing epoch, and the services fence off stale epochs (HTTP 409). A deposed leader is
refused by the services rather than trusted to stand down. More in
[docs/architecture.md](docs/architecture.md).

## Key features

- **Branch-tree journal** on SQLite in WAL mode with `synchronous=FULL`: intent is durable
  before every effect, so recovery is never blind.
- **Divergent recovery**: forks at the decision, picks the highest-ranked untried
  alternative, and is bounded (fork depth 3, then escalate) so it cannot livelock.
- **The bounded irreversible barrier**, with saga-style LIFO compensation and escalation
  on exhaustion.
- **Irreversible residue**: a page that already rang on an abandoned branch is recorded as
  permanent residue, and the corrected page carries a *supersede* annotation naming it.
- **Bounded ambiguity**: timeouts on unobservable effects become a surfaced `unknown`. A
  page that lands late is caught on its idempotency key instead of paging twice.
- **Failover with fencing tokens**: lease + epoch in the journal, enforced by the services.
- **At-least-once ingest absorbed**: the workflow id is derived from the alert id, so a
  redelivered alert lands on the same workflow and replays to a no-op.
- **An oracle, not a self-assessment**: the EEO checker compares the journal with the
  out-of-process ledger, and a **crash sweep** injects a crash at every step boundary under
  five fault modes.
- **Same engine, two worlds**: `InProcessWorld` and `HttpWorld` implement one `World`
  protocol, and the tests assert they produce identical outcomes.
- **Observability**: a live dashboard (gate state, branch tree, scoreboard, phones) and a
  Markdown incident post-mortem rendered from the branch tree.

## Tech stack

| Area | Technology | Why |
|---|---|---|
| Language | Python 3.10+ | dataclasses and `Protocol` for the frozen interfaces |
| Durable state | SQLite in WAL mode (`sqlite3`, stdlib) | one file, crash-safe, shared by both orchestrators |
| Effect services, ledger, dashboard | FastAPI + uvicorn + pydantic | small, typed HTTP services; one app parameterised by service |
| HTTP client | httpx | separate connect vs read timeouts, which is what distinguishes `failed` from `unknown` |
| Ingest | Redis Streams (redis-py), Docker Compose | consumer groups, pending-entry lists, `XAUTOCLAIM` for dead consumers |
| Dashboard UI | one HTML page, vanilla JS + inline SVG | polls the read-only API; no build step |
| Testing | pytest (351 tests), ruff | unit, integration, multi-process and failover tests |
| Ops | `scripts/verify.sh` (bash), `scripts/verify.bat` (Windows + WSL) | one-command end-to-end verification |

The kernel (journal, branch tree, barrier, compensation, EEO checker, in-process world) is
**standard library only**: the core demo runs with no third-party packages at all.

## Project structure

![Code architecture](docs/images/code-architecture.drawio.svg)

```
src/sluice/
├── core/            durable-execution kernel (stdlib only)
│   ├── types.py         frozen interfaces: EffectType, ToolResult, JournalRecord, Branch, World
│   ├── journal.py       SQLite-WAL branch tree, leases, fencing epochs
│   ├── tools.py         the incident-triage workflow: 8 tools, effect types, scripted agent
│   └── engine.py        orchestrator: replay, divergence, barrier, compensation, escalation
├── world/           effect layer behind the World protocol
│   ├── inprocess.py     InProcessWorld: in-memory effects + fault injection
│   ├── faults.py        FaultConfig and named presets (poison, zombie, clear)
│   ├── ledger.py        the ground-truth ledger
│   ├── http_client.py   HttpWorld, HttpLedger: same protocol over HTTP
│   └── services.py      FastAPI effect services and ledger service
├── ingest.py        alert sources: Redis Streams consumer group, in-process fallback
├── observability/   journal -> dashboard state (view.py), incident post-mortem (audit.py)
├── verification/    EEO checker, crash sweep, overhead benchmark
├── scenarios/       the seven runnable scenarios, and the failover demo
├── dashboard/       read-only FastAPI dashboard + static page
└── cli/             the `sluice` command, one module per subcommand
tests/
├── unit/            one module at a time, synthetic inputs (257 tests)
└── integration/     scenarios, real sockets, Redis, multi-process failover (94 tests)
scripts/             verify.sh (Linux/WSL), verify.bat (Windows host)
docs/                architecture, design, verification, WSL + Docker, CLI, testing
```

## Quick start

Requires Python 3.10+. On Windows, run everything inside WSL2 (see
[docs/wsl-docker.md](docs/wsl-docker.md)).

```bash
git clone <this repo> && cd signal_hack
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"            # the `sluice` command, HTTP + Redis extras, pytest

sluice demo                    # the poison step, three recovery strategies side by side
```

No install at all is needed for the core demo: `PYTHONPATH=src python3 -m sluice demo`
runs on the standard library alone.

It runs the same failure through all three strategies. Pinned ends at `tickets 1 posts 1
pages 0` (livelocked), naive at `tickets 2 posts 2 pages 1` (duplicates), and Sluice:

```
--- SLUICE      poison ---
  outcome:       completed
  tickets 1   posts 1   pages 1      (gross 2/2/1)
  phone rang:    rota-Y
  gate:          [RELEASED] cleanup proven, gate lifted
  EEO:           PASS
...
  unexplained violations:    0    <- this is the number that matters
```

`gross 2/2/1` is the honest part: the abandoned branch's ticket and post really happened,
and were compensated.

More scenarios:

```bash
sluice demo --scenario residue         # the wrong page already rang: residue + supersede
sluice demo --scenario compfail        # cleanup fails for good: the barrier escalates
sluice demo --scenario compretry       # cleanup fails once, then succeeds
sluice demo --scenario zombie          # the page times out, is surfaced, lands late
sluice demo --scenario crash           # crash between an effect and its journal record
sluice demo --scenario all --flush-late
sluice demo --slow 0.35                # paced and narrated, for watching
sluice demo --sweep                    # the crash sweep and evidence table
sluice demo --failover                 # lease, epoch, fencing token
sluice demo --bench                    # what durability costs per step
```

The dashboard, in a second terminal:

```bash
sluice dashboard --allow-control   # http://127.0.0.1:8000, with Run buttons
```

## Running it as a distributed system

```bash
docker compose up -d                   # Redis on :6379 (run from the repo root)
sluice services                        # ledger + ticket + channel + pager, 4 processes
sluice smoke                           # 20 checks over real sockets and the stream

sluice faults poison                   # rota-X has nobody on call
sluice producer --demo --count 1       # publish the alert
sluice orchestrator --once --narrate
```

The orchestrator consumes from Redis, drives the services over HTTP, journals to
`.sluice/shared.db`, and grades itself against the ledger:

```
[ingest] redis stream alerts:incoming group orchestrators
    [orch-a] barrier_blocked 2 uncompensated effects on abandoned branch
    [orch-a] compensated post_to_channel
    [orch-a] compensated create_ticket
    [orch-a] barrier_released page_oncall
  a-1001: completed   rota=rota-Y       146ms  EEO PASS
```

For a real leader race, run two orchestrators with different `--owner` values against the
same `--db`, and `kill -9` the leader mid-workflow. The standby reclaims the stalled alert
from Redis (`XAUTOCLAIM`), takes the lease once the dead leader's expires, resumes from the
journal at a higher epoch, and finishes with exactly one page. The test suite automates
exactly this, killing the leader in the middle of compensation.

## Testing and verification

```bash
pytest                      # 351 tests: unit + integration (spawns its own services)
pytest tests/unit           # the fast layer, a few seconds
scripts/verify.sh           # everything, end to end, against real Redis in Docker
```

- **Unit tests** pin each module with synthetic inputs, so a failure names the rule that
  broke: each EEO clause, the barrier scope, LIFO order, lease epochs, fault modes.
- **Integration tests** run every scenario under every strategy (21 panes, journal
  cross-checked against the ledger) and exercise FastAPI apps through TestClient. They also
  launch the four services as real processes on free ports, use Redis (DB 15, never your
  data), and run the full producer → Redis → orchestrator → HTTP pipeline, including a
  `kill -9` of the leader mid-compensation.
- **Architecture guards** fail the build if a layer imports upward, if the kernel gains a
  third-party import, or if `requirements.txt` drifts from `pyproject.toml`.
- **`scripts/verify.sh`** cleans stale state, starts Redis and the services, runs the
  suite, the demo, smoke, and the Redis pipeline (fresh run, replay, redelivery), then
  tears everything down. Expected last line: `ALL CHECKS PASSED`.

See [docs/testing.md](docs/testing.md) for the test map and a root-cause playbook, and
[docs/verification.md](docs/verification.md) for what each verification step proves.

## Results

**Every scenario, every strategy** (`sluice demo --scenario all`):

| Scenario | Stresses | Pinned | Naive | **Sluice** |
|---|---|---|---|---|
| poison | wrong class, page fails | 1/1/0 livelock · FAIL | 2/2/1 · FAIL | **1/1/1 · PASS** |
| residue | wrong page already rang | 1/1/1 · PASS* | 2/2/2 · FAIL | **1/1/2 · PASS**, supersedes |
| compfail | cleanup fails for good | livelock · FAIL | 2/2/1 · FAIL | **escalates · PASS** |
| compretry | cleanup fails once | livelock · FAIL | 2/2/1 · FAIL | **1/1/1 · PASS** |
| zombie | page times out, lands late | escalates · PASS | escalates · PASS | **escalates · PASS** |
| crash | crash at a step boundary | livelock · FAIL | 2/2/1 · FAIL | **1/1/1 · PASS** |
| redelivery | same alert twice | 1/1/1 · PASS | 2/2/2 · FAIL | **1/1/1 · PASS** |

\* Pinned passes `residue` while paging the wrong rota: EEO is a claim about effects, not
judgement. That is why the branch tree and the barrier exist.

**Crash sweep** (`sluice demo --sweep`): a crash at every step boundary (8 steps × 4
phases + a clean run = 33 crash points) under 5 fault modes:

```
total runs:                165
EEO clause 1 (no dup):     pass 165 / 165
EEO clause 2 (no loss):    pass 165 / 165
EEO clause 3 (clean abd):  pass 165 / 165
escalation rate:           60.0%      crash 0% · partition-transient 0% · timeout, partition,
                                      late-delivery 100% (permanent faults, by construction)
unexplained violations:    0
```

**Overhead** (`sluice demo --bench`, measured on WSL2 / ext4): durability costs
**~3.3 ms per step**, ~92 % of it the per-commit fsync that keeps an `INTENT` record from
being lost. Against a realistic effect layer (25 ms per call) that is **1.26× (+26 %)**.

## Where it falls over

- **Exactly-once for irreversible + unobservable effects is impossible** (two generals).
  The ambiguity is bounded to one place and reported, never silently resolved.
- **Compensation restores state, not history.** A deleted channel post was still seen.
- **The barrier is bounded, not absolute.** When cleanup cannot complete it escalates to a
  human. That is a degraded outcome, chosen deliberately over hanging or acting blind.
- **The agent is scripted.** Decisions come from a scripted trace with ranked
  alternatives; there is no live model call.
- **Single host.** Two real orchestrator processes race for one lease, but on one machine;
  partitions between hosts are not exercised.
- **Fault coverage is not proof.** The sweep covers crash, timeout, partition and late
  delivery at every boundary, not byzantine services, clock skew or disk corruption.

## Documentation

| Document | What is in it |
|---|---|
| [docs/architecture.md](docs/architecture.md) | components, data model, journal schema, the recovery algorithm, fencing |
| [docs/design.md](docs/design.md) | the design decisions and the scenario analysis, in two pages |
| [docs/cli.md](docs/cli.md) | every command and flag, environment variables, HTTP APIs, tuning knobs |
| [docs/testing.md](docs/testing.md) | test map, how to run subsets, root-cause playbook |
| [docs/verification.md](docs/verification.md) | `verify.sh` step by step, expected output, reading failures |
| [docs/wsl-docker.md](docs/wsl-docker.md) | running on WSL2 with Docker, and the pitfalls already hit |
