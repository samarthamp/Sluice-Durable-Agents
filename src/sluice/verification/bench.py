"""The overhead benchmark: what does durable execution cost per decision?"""

from __future__ import annotations

import os
import shutil
import statistics
import tempfile
import threading
import time

from ..core.engine import Orchestrator
from ..core.journal import Journal
from ..core.tools import trace_for
from ..core.types import Alert, effect_key, workflow_id_for
from ..world import FaultConfig, GroundTruthLedger, InProcessWorld

STEPS_PER_WORKFLOW = 8


def _alert(i: int) -> Alert:
    return Alert(f"a-bench-{i}", "payments-api", "prometheus", {"msg": "bench"})


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = min(len(ordered) - 1, max(0, int(round((p / 100.0) * (len(ordered) - 1)))))
    return ordered[k]


def _stats(samples: list[float]) -> dict:
    return {
        "n": len(samples),
        "mean_ms": 1000 * statistics.fmean(samples) if samples else 0.0,
        "p50_ms": 1000 * _percentile(samples, 50),
        "p95_ms": 1000 * _percentile(samples, 95),
        "p99_ms": 1000 * _percentile(samples, 99),
        "total_s": sum(samples),
    }


def _fresh_world(effect_latency_ms: float = 0.0) -> InProcessWorld:
    # No faults: a clean run, so we price the happy path.
    faults = FaultConfig()
    faults.latency_s = effect_latency_ms / 1000.0
    return InProcessWorld(GroundTruthLedger(), faults)


def _bare(n: int, warmup: int, effect_latency_ms: float = 0.0) -> dict:
    """The floor: the same eight effects, issued directly, with no durability."""
    world = _fresh_world(effect_latency_ms)
    samples: list[float] = []
    for i in range(n + warmup):
        alert = _alert(i)
        wf = workflow_id_for(alert.alert_id)
        steps = trace_for(alert, "P1")
        t0 = time.perf_counter()
        for seq, step in enumerate(steps):
            world.execute(
                step["tool"], step["args"], effect_key(wf, "br-bare", seq), 1, 2.0
            )
        elapsed = time.perf_counter() - t0
        if i >= warmup:
            samples.append(elapsed)
    return _stats(samples)


def _journaled(
    db: str, synchronous: str, n: int, warmup: int, effect_latency_ms: float = 0.0
) -> dict:
    """One journal, many workflows -- the steady state, not the cold start."""
    world = _fresh_world(effect_latency_ms)
    journal = Journal(db, synchronous=synchronous)
    samples: list[float] = []
    try:
        for i in range(n + warmup):
            alert = _alert(i)
            orch = Orchestrator(journal, world, owner="orch-bench", mode="sluice")
            t0 = time.perf_counter()
            orch.run(alert, "P1")
            elapsed = time.perf_counter() - t0
            if i >= warmup:
                samples.append(elapsed)

        out = _stats(samples)
        last_wf = workflow_id_for(_alert(n + warmup - 1).alert_id)
        out["records_per_workflow"] = len(journal.records(last_wf))
    finally:
        journal.close()

    out["bytes_on_disk"] = sum(
        os.path.getsize(db + s) for s in ("", "-wal", "-shm") if os.path.exists(db + s)
    )
    out["bytes_per_workflow"] = out["bytes_on_disk"] / max(1, n + warmup)
    return out


def _concurrent(db: str, threads: int, per_thread: int) -> dict:
    """Single-process contention on one shared journal."""
    world = _fresh_world()
    journal = Journal(db, synchronous="FULL")
    samples: list[list[float]] = [[] for _ in range(threads)]
    barrier = threading.Barrier(threads)

    def worker(t: int) -> None:
        barrier.wait()
        for i in range(per_thread):
            alert = _alert(100000 + t * per_thread + i)
            orch = Orchestrator(journal, world, owner=f"orch-bench-{t}", mode="sluice")
            t0 = time.perf_counter()
            orch.run(alert, "P1")
            samples[t].append(time.perf_counter() - t0)

    workers = [threading.Thread(target=worker, args=(t,)) for t in range(threads)]
    started = time.perf_counter()
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    wall = time.perf_counter() - started
    journal.close()

    flat = [s for row in samples for s in row]
    out = _stats(flat)
    out["threads"] = threads
    out["wall_s"] = wall
    out["throughput_per_s"] = len(flat) / wall if wall else 0.0
    return out


def benchmark(
    n: int = 200,
    warmup: int = 20,
    concurrency: int = 0,
    effect_latency_ms: float = 25.0,
    workdir: str | None = None,
) -> dict:
    """Measure durability overhead against an in-process floor and a realistic
    effect layer."""
    owned = workdir is None
    workdir = workdir or tempfile.mkdtemp(prefix="sluice-bench-")
    os.makedirs(workdir, exist_ok=True)

    # Latency rows cost 8x the effect latency per workflow, so they get a smaller sample.
    lat_n = max(20, n // 5)
    lat_warmup = max(3, warmup // 4)

    try:
        rows = {
            "bare": _bare(n, warmup),
            "memory": _journaled(":memory:", "FULL", n, warmup),
            "normal": _journaled(os.path.join(workdir, "normal.db"), "NORMAL", n, warmup),
            "full": _journaled(os.path.join(workdir, "full.db"), "FULL", n, warmup),
        }
        latency_rows = None
        if effect_latency_ms > 0:
            latency_rows = {
                "bare_lat": _bare(lat_n, lat_warmup, effect_latency_ms),
                "full_lat": _journaled(
                    os.path.join(workdir, "full_lat.db"), "FULL",
                    lat_n, lat_warmup, effect_latency_ms,
                ),
            }
        concurrent = (
            _concurrent(os.path.join(workdir, "concurrent.db"), concurrency, max(10, n // 4))
            if concurrency > 1
            else None
        )
    finally:
        if owned:
            shutil.rmtree(workdir, ignore_errors=True)

    bare = rows["bare"]["mean_ms"]
    full = rows["full"]["mean_ms"]
    normal = rows["normal"]["mean_ms"]
    memory = rows["memory"]["mean_ms"]

    overhead = full - bare
    out = {
        "n": n,
        "warmup": warmup,
        "rows": rows,
        "latency_rows": latency_rows,
        "effect_latency_ms": effect_latency_ms,
        "latency_n": lat_n,
        "concurrent": concurrent,
        "overhead_ms": overhead,
        "overhead_per_decision_ms": overhead / STEPS_PER_WORKFLOW,
        "multiple_of_bare": (full / bare) if bare else float("inf"),
        "fsync_ms": full - normal,
        "fsync_share": ((full - normal) / overhead) if overhead > 0 else 0.0,
        "bookkeeping_ms": memory - bare,
        "throughput_per_s": 1000.0 / full if full else 0.0,
    }

    if latency_rows:
        bare_lat = latency_rows["bare_lat"]["mean_ms"]
        full_lat = latency_rows["full_lat"]["mean_ms"]
        out["realistic_overhead_ms"] = full_lat - bare_lat
        out["realistic_multiple"] = (full_lat / bare_lat) if bare_lat else float("inf")
        out["realistic_overhead_pct"] = (
            100.0 * (full_lat - bare_lat) / bare_lat if bare_lat else 0.0
        )
    return out


def format_benchmark(r: dict) -> str:
    rows = r["rows"]
    labels = {
        "bare": "bare tool calls",
        "memory": "+ journal (:memory:)",
        "normal": "+ journal, synchronous=NORMAL",
        "full": "+ journal, synchronous=FULL",
    }
    notes = {
        "bare": "floor, no durability",
        "memory": "bookkeeping only",
        "normal": "on disk, no fsync per commit",
        "full": "what we ship",
    }

    lines = [
        "OVERHEAD BENCHMARK",
        "",
        f"  workflows measured:        {r['n']}"
        f"   ({STEPS_PER_WORKFLOW} steps each, clean run, no faults)",
        f"  warmup discarded:          {r['warmup']}",
        "  effect layer:              in-process (isolates durability cost from network)",
        "",
        f"  {'':<30}{'mean':>9}{'p50':>9}{'p95':>9}{'p99':>9}   note",
    ]
    for key in ("bare", "memory", "normal", "full"):
        s = rows[key]
        lines.append(
            f"  {labels[key]:<30}{s['mean_ms']:>8.3f}ms{s['p50_ms']:>8.3f}ms"
            f"{s['p95_ms']:>8.3f}ms{s['p99_ms']:>8.3f}ms   {notes[key]}"
        )

    full = rows["full"]
    lines += [
        "",
        f"  durable execution overhead:  {r['overhead_ms']:.3f} ms per workflow",
        f"                               {r['overhead_per_decision_ms']:.3f} ms per step"
        "   <- quote this one",
        f"  of which fsync:              {r['fsync_ms']:.3f} ms"
        f"  ({100 * r['fsync_share']:.0f}% of the overhead)",
        f"  of which bookkeeping:        {r['bookkeeping_ms']:.3f} ms",
        "",
        f"  journal size:                {full.get('records_per_workflow', 0)} records,"
        f" {full.get('bytes_per_workflow', 0):.0f} bytes per workflow",
        f"  sequential throughput:       {r['throughput_per_s']:.0f} workflows/sec",
    ]

    lat = r.get("latency_rows")
    if lat:
        ms = r["effect_latency_ms"]
        lines += [
            "",
            f"  AGAINST A REALISTIC EFFECT LAYER  ({ms:.0f}ms injected per effect call,"
            f" {r['latency_n']} workflows)",
            f"  {'':<30}{'mean':>9}",
            f"  {'bare + ' + str(int(ms)) + 'ms/effect':<30}"
            f"{lat['bare_lat']['mean_ms']:>8.1f}ms   what the work costs anyway",
            f"  {'+ journal, synchronous=FULL':<30}"
            f"{lat['full_lat']['mean_ms']:>8.1f}ms   durable",
            "",
            f"  same overhead, honest denominator:  "
            f"{r['realistic_overhead_ms']:.1f} ms"
            f"  ({r['realistic_multiple']:.2f}x, +{r['realistic_overhead_pct']:.0f}%)",
            "",
            f"  The {r['multiple_of_bare']:.0f}x figure above is real but meaningless: the",
            "  in-process floor is eight dict writes, and nothing pages an engineer that",
            "  way.  The latency is simulated, but it is injected identically into both",
            "  rows, so it cancels and the ratio is measured rather than extrapolated.",
            "  On Windows, sleep granularity is ~15ms, so the absolute figures here are",
            "  coarse; the ratio is not affected because both rows pay the same cost.",
        ]

    c = r.get("concurrent")
    if c:
        lines += [
            "",
            f"  {c['threads']} threads, one shared journal:"
            f"  {c['throughput_per_s']:.0f} workflows/sec,"
            f" p99 {c['p99_ms']:.1f}ms",
            "  (single-process SQLite contention, NOT a distributed scale run)",
        ]

    lines += [
        "",
        "  The fsync line is the price of correctness, not waste: a crash between the",
        "  INTENT record and the effect must not lose the INTENT record, or recovery",
        "  cannot know the step was started. synchronous=NORMAL is a supported knob and",
        "  the row above is what it buys back.",
        "",
        "  Known scaling limit, not measured here: the barrier predicate scans a",
        "  workflow's records on every irreversible step. It is indexed per workflow so",
        "  it does not degrade with workflow count, but it is O(records) within one.",
    ]
    return "\n".join(lines)
