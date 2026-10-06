"""The EEO checker (2.7) and the evidence table (2.8)."""

from __future__ import annotations

import json
import os
import tempfile
import time
from itertools import pairwise

from ..core.journal import Journal
from ..core.tools import EFFECT_TYPES
from ..world.ledger import GroundTruthLedger

CLAUSES = ("no_duplication", "no_loss", "clean_abandonment", "bounded_ambiguity")


class Violation:
    def __init__(self, clause: str, message: str, explained: bool = False, why: str = ""):
        self.clause = clause
        self.message = message
        self.explained = explained
        self.why = why

    def to_dict(self) -> dict:
        return {
            "clause": self.clause,
            "message": self.message,
            "explained": self.explained,
            "why": self.why,
        }

    def __repr__(self) -> str:
        tag = "explained" if self.explained else "UNEXPLAINED"
        return f"<{self.clause} [{tag}] {self.message}>"


def _escalated_keys(recs) -> set[str]:
    """Keys the system explicitly surfaced as uncompensated in an escalation."""
    out: set[str] = set()
    for r in recs:
        if r.kind != "ESCALATED" or not r.detail:
            continue
        for item in r.detail.get("uncompensated") or []:
            if isinstance(item, dict) and item.get("key"):
                out.add(item["key"])
    return out


def _clause_1_no_duplication(recs, effects, violations) -> None:
    per_key: dict[str, int] = {}
    for e in effects:
        per_key[e["key"]] = per_key.get(e["key"], 0) + 1
    for key, n in sorted(per_key.items()):
        if n > 1:
            violations.append(
                Violation("no_duplication", f"idempotency key {key} committed {n} times")
            )

    # A second page is legitimate only when it explains the first.
    irreversible = sorted(
        [
            e
            for e in effects
            if EFFECT_TYPES.get(e["tool"]) is not None
            and EFFECT_TYPES[e["tool"]].reversibility == "irreversible"
        ],
        key=lambda e: e["ts"],
    )
    for i, later in enumerate(irreversible[1:], start=1):
        args = later.get("args") or {}
        supersedes = args.get("supersedes")
        target = args.get("rota") or later["tool"]
        if not supersedes:
            violations.append(
                Violation(
                    "no_duplication",
                    f"{later['tool']} #{i + 1} (to {target}) carries no supersede"
                    f" annotation for the {i} earlier irreversible effect(s)",
                )
            )
        elif not any(
            supersedes.get("rota") == (prior.get("args") or {}).get("rota")
            for prior in irreversible[:i]
        ):
            violations.append(
                Violation(
                    "no_duplication",
                    f"{later['tool']} to {target} supersedes"
                    f" {supersedes.get('rota')}, which was never committed",
                )
            )


def _clause_2_no_loss(recs, effects, violations, outcome) -> None:
    committed_action = any(
        e["tool"] == "page_oncall" for e in effects
    ) or outcome == "completed"
    surfaced = any(r.kind == "ESCALATED" for r in recs)
    if not (committed_action or surfaced):
        violations.append(
            Violation(
                "no_loss",
                f"workflow ended in {outcome!r} with neither a committed action nor a"
                " surfaced escalation",
            )
        )


def _clause_3_clean_abandonment(journal, workflow_id, recs, violations) -> None:
    escalated = _escalated_keys(recs)

    pending = journal.uncompensated(workflow_id)
    if pending:
        keys = {p.key for p in pending}
        covered = keys <= escalated
        violations.append(
            Violation(
                "clean_abandonment",
                f"{len(pending)} uncompensated on abandoned branch:"
                f" {[p.tool_name for p in pending]}",
                explained=covered,
                why=(
                    "escalation record names these exact effects; the barrier"
                    " escalated rather than acting blind or hanging (2.3)"
                    if covered
                    else ""
                ),
            )
        )

    # No effect on a still-active branch may be compensated.
    live = {b.branch_id for b in journal.branches(workflow_id) if b.status == "active"}
    origin_branch = {
        r.key: r.branch_id
        for r in recs
        if r.kind == "RESULT" and r.result and r.result.status == "ok"
    }
    for r in recs:
        if r.kind != "COMP_RESULT" or not r.result or r.result.status != "ok":
            continue
        if origin_branch.get(r.key) in live:
            violations.append(
                Violation(
                    "clean_abandonment",
                    f"compensated {r.tool_name} on live branch {origin_branch[r.key]}",
                )
            )

    # Reverse execution order, LIFO, saga-style (2.6).
    origin_rid = {
        r.key: r.record_id
        for r in recs
        if r.kind == "RESULT" and r.result and r.result.status == "ok"
    }
    attempts: dict[tuple[str, int], list[int]] = {}
    for r in recs:
        if r.kind != "COMP_RESULT" or not r.result or r.result.status != "ok":
            continue
        attempt = (r.detail or {}).get("attempt", 1)
        attempts.setdefault((r.branch_id, attempt), []).append(origin_rid.get(r.key, -1))
    for (branch_id, attempt), rids in attempts.items():
        if any(a <= b for a, b in pairwise(rids)):
            violations.append(
                Violation(
                    "clean_abandonment",
                    f"compensation on {branch_id} attempt {attempt} did not run in"
                    f" reverse execution order: {rids}",
                )
            )


def _bounded_ambiguity(recs, violations) -> int:
    """At most one irreversible+unobservable effect in unknown state, and surfaced."""
    unknown_now: dict[tuple[str, int], object] = {}
    for r in recs:
        if r.kind not in ("RESULT", "LATE_DELIVERY_SUPPRESSED"):
            continue
        slot = (r.branch_id, r.seq)
        if r.kind == "LATE_DELIVERY_SUPPRESSED":
            unknown_now.pop(slot, None)
            continue
        if (
            r.result
            and r.result.status == "unknown"
            and r.effect_type
            and r.effect_type.reversibility == "irreversible"
            and r.effect_type.observability == "unobservable"
        ):
            unknown_now[slot] = r
        else:
            unknown_now.pop(slot, None)

    ba = len(unknown_now)
    surfaced = any(r.kind in ("ESCALATED", "LATE_DELIVERY_SUPPRESSED") for r in recs)

    if ba > 1:
        violations.append(
            Violation("bounded_ambiguity", f"{ba} irreversible effects in unknown state at once")
        )
    if ba and not surfaced:
        violations.append(
            Violation("bounded_ambiguity", "unknown irreversible effect was never surfaced")
        )
    return ba


def check_eeo(
    journal: Journal,
    ledger: GroundTruthLedger,
    workflow_id: str,
    outcome: str = "",
) -> dict:
    recs = journal.records(workflow_id)
    effects = ledger.effects(workflow_id=workflow_id)
    violations: list[Violation] = []

    _clause_1_no_duplication(recs, effects, violations)
    _clause_2_no_loss(recs, effects, violations, outcome)
    _clause_3_clean_abandonment(journal, workflow_id, recs, violations)
    ba = _bounded_ambiguity(recs, violations)

    unexplained = [v for v in violations if not v.explained]
    clauses = {c: True for c in CLAUSES}
    for v in violations:
        if not v.explained:
            clauses[v.clause] = False

    return {
        "workflow_id": workflow_id,
        "outcome": outcome,
        "pass": not unexplained,
        "clauses": clauses,
        # Carry the reasons so the escalation rate can be broken down, not just quoted.
        "escalations": [
            (r.detail or {}).get("reason", "unspecified") for r in recs if r.kind == "ESCALATED"
        ],
        "violations": [v.to_dict() for v in violations],
        "unexplained": [v.to_dict() for v in unexplained],
        "ba_surfaced": ba,
        "counts": ledger.counts(workflow_id=workflow_id),
        "gross": ledger.gross_counts(workflow_id=workflow_id),
        "scoreboard": ledger.scoreboard(workflow_id=workflow_id),
        "residue": [
            {"tool": r.tool_name, "args": r.args, "branch_id": r.branch_id}
            for r in journal.residue(workflow_id)
        ],
    }


def explain(result: dict) -> str:
    lines = [f"EEO verdict for {result['workflow_id']}: "
             f"{'PASS' if result['pass'] else 'FAIL'}"]
    for v in result["violations"]:
        tag = "explained  " if v["explained"] else "UNEXPLAINED"
        lines.append(f"  [{tag}] {v['clause']}: {v['message']}")
        if v["why"]:
            lines.append(f"                {v['why']}")
    if result["ba_surfaced"]:
        lines.append(f"  bounded ambiguity surfaced: {result['ba_surfaced']}")
    return "\n".join(lines)


def outcome_counts(results: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in results:
        outcome = r.get("result_outcome") or r.get("outcome") or "unknown"
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def escalation_reasons(results: list[dict]) -> dict[str, int]:
    reasons: dict[str, int] = {}
    for r in results:
        for reason in r.get("escalations", []) or []:
            reasons[reason] = reasons.get(reason, 0) + 1
    return reasons


def evidence_table(
    results: list[dict],
    crash_points: int | None = None,
    fault_modes: list[str] | None = None,
    sweep: bool | None = None,
) -> str:
    """The evidence slide, emitted directly rather than assembled by hand (2.8)."""
    if sweep is None:
        sweep = crash_points is not None or fault_modes is not None
    passed = {c: 0 for c in CLAUSES}
    ba = 0
    unexplained = 0

    for r in results:
        ba += r.get("ba_surfaced", 0)
        unexplained += len(r.get("unexplained", []))
        for clause, ok in r.get("clauses", {}).items():
            if ok:
                passed[clause] = passed.get(clause, 0) + 1

    n = len(results)
    outcomes = outcome_counts(results)
    acted = outcomes.get("completed", 0)
    escalated = outcomes.get("escalated", 0)
    # Anything that is neither acted nor escalated has, by clause 2, lost the signal.
    neither = n - acted - escalated
    pct = (100.0 * escalated / n) if n else 0.0

    lines = ["CRASH SWEEP RESULTS" if sweep else f"EEO VERDICT  ({n} run(s))", ""]
    if crash_points is not None:
        lines.append(f"  crash points swept:        {crash_points}"
                     "   (every step boundary x every branch state)")
    if fault_modes is not None:
        lines.append(f"  fault modes:               {len(fault_modes)}"
                     f"   ({', '.join(fault_modes)})")
    lines += [
        f"  total runs:                {n}",
        "",
        f"  EEO clause 1 (no dup):     pass {passed['no_duplication']} / {n}",
        f"  EEO clause 2 (no loss):    pass {passed['no_loss']} / {n}",
        f"  EEO clause 3 (clean abd):  pass {passed['clean_abandonment']} / {n}",
        "",
        "  terminal outcomes",
        f"    acted, cleanup proven:   {acted} / {n}",
        f"    escalated to a human:    {escalated} / {n}",
        f"    neither:                 {neither} / {n}"
        f"    {'<- each one is a clause 2 loss' if neither else ''}",
        "",
    ]
    if sweep:
        lines.append(
            f"  escalation rate:           {pct:.1f}%"
            "   <- the automation-vs-human tradeoff, measured"
        )
    else:
        lines.append(
            "  escalation rate:           (not a rate at this sample size;"
            " run --sweep)"
        )
    lines += [
        f"  BA surfaced (unknown):     {ba}    <- expected, these are the hard corner",
        f"  unexplained violations:    {unexplained}    <- this is the number that matters",
    ]
    return "\n".join(lines)


def format_escalations(results: list[dict]) -> str:
    """Why a human was pulled in, not just how often."""
    reasons = escalation_reasons(results)
    if not reasons:
        return "no escalations"
    total = sum(reasons.values())
    lines = [f"WHY IT ESCALATED  ({total} escalation records)"]
    for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {count:>4}  {reason}")
    return "\n".join(lines)


# ---------------------------------------------------------------- verdict sidecar
#
# Grading needs the ledger, which the read-only dashboard does not have. Whichever
# process held the ledger writes its verdict beside the journal for display.

VERDICT_SUFFIX = ".verdict.json"


def verdict_path(db: str) -> str:
    return db + VERDICT_SUFFIX


def write_verdict(db: str, verdict: dict, mode: str = "") -> None:
    """Best effort: a dashboard nicety must never break a run that otherwise worked."""
    payload = {
        "workflow_id": verdict.get("workflow_id", ""),
        "mode": mode,
        "outcome": verdict.get("outcome", ""),
        "pass": bool(verdict.get("pass")),
        "unexplained": [
            {"clause": v.get("clause", ""), "message": v.get("message", "")}
            for v in verdict.get("unexplained", [])
        ],
        "ba_surfaced": bool(verdict.get("ba_surfaced")),
        "escalations": list(verdict.get("escalations", [])),
        "ts": time.time(),
    }
    try:
        # Written atomically: the dashboard polls this file several times a second and
        # must never read a half-written one.
        d = os.path.dirname(os.path.abspath(db)) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, verdict_path(db))
    except Exception:
        pass


def read_verdict(db: str, workflow_id: str = "") -> dict | None:
    """Return the sidecar verdict, or None."""
    try:
        with open(verdict_path(db), encoding="utf-8") as fh:
            v = json.load(fh)
    except Exception:
        return None
    if workflow_id and v.get("workflow_id") and v["workflow_id"] != workflow_id:
        return None
    return v
