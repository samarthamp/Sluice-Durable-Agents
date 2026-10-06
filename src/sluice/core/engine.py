"""The orchestrator: journal, branch tree, barrier, compensation driver, escalation."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .journal import Journal, LeaseUnavailable
from .tools import (
    ALTERNATIVES,
    CLASSIFY_SEQ,
    EFFECT_TYPES,
    PAGE_SEQ,
    supersede_annotation,
    trace_for,
)
from .types import Alert, ToolResult, World, effect_key, workflow_id_for

# Bounded divergence (2.5).
MAX_FORK_DEPTH = 3

# Bounded barrier (2.3).  Compensation is retried to a deadline, then escalates.
MAX_COMP_ATTEMPTS = 3
BARRIER_DEADLINE_S = 5.0
COMP_BACKOFF_S = 0.05

TIMEOUT_S = 2.0
LEASE_TTL_S = 30.0

CRASH_PHASES = ("before_intent", "after_intent", "after_effect", "after_result")

# Capability matrix.
MODES: dict[str, dict[str, bool]] = {
    "pinned": {"replays": True, "diverges": False, "barrier": False, "compensates": False},
    "naive": {"replays": False, "diverges": True, "barrier": False, "compensates": False},
    "sluice": {"replays": True, "diverges": True, "barrier": True, "compensates": True},
}


class Escalation(Exception):
    """Terminal, surfaced, non-silent.  Satisfies the liveness clause of EEO (2.7)."""

    def __init__(self, reason: str, detail: dict):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class ProcessCrash(Exception):
    """Injected process death."""


class _Diverge(Exception):
    def __init__(self, reason: str, fork_point_record_id: int | None):
        super().__init__(reason)
        self.reason = reason
        self.fork_point_record_id = fork_point_record_id


@dataclass
class CrashPolicy:
    """Crash at a chosen step boundary."""

    at_seq: int | None = None
    phase: str = "after_effect"
    armed: bool = True
    fired_at: tuple[int, str] | None = field(default=None, compare=False)

    def check(self, seq: int, phase: str) -> None:
        if not self.armed or self.at_seq != seq or self.phase != phase:
            return
        self.armed = False
        self.fired_at = (seq, phase)
        raise ProcessCrash(f"crash injected at seq {seq} ({phase})")

    def label(self) -> str:
        return f"seq{self.at_seq}:{self.phase}" if self.at_seq is not None else "none"


class Orchestrator:
    def __init__(
        self,
        journal: Journal,
        world: World,
        owner: str = "orch-a",
        mode: str = "sluice",
        crash: CrashPolicy | None = None,
        on_event=None,
        step_pause_s: float = 0.0,
        lease_ttl_s: float = LEASE_TTL_S,
        renew_lease: bool = True,
        diverge_after_seq: int | None = None,
        diverge_reason: str = "misclassification discovered after commit",
        max_fork_depth: int = MAX_FORK_DEPTH,
        max_comp_attempts: int = MAX_COMP_ATTEMPTS,
        barrier_deadline_s: float = BARRIER_DEADLINE_S,
        timeout_s: float = TIMEOUT_S,
    ):
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}, expected one of {sorted(MODES)}")
        self.j = journal
        self.world = world
        self.owner = owner
        self.mode = mode
        self.caps = MODES[mode]
        self.epoch = 1
        self.crash = crash
        self.on_event = on_event
        self.step_pause_s = step_pause_s
        self.lease_ttl_s = lease_ttl_s
        self.renew_lease = renew_lease
        self.diverge_after_seq = diverge_after_seq
        self.diverge_reason = diverge_reason
        self._diverge_armed = diverge_after_seq is not None
        # Bounds are instance state so the sweep can shrink the deadline without editing
        # the module constants.
        self.max_fork_depth = max_fork_depth
        self.max_comp_attempts = max_comp_attempts
        self.barrier_deadline_s = barrier_deadline_s
        self.timeout_s = timeout_s
        # Filled from the decision step's own alternatives when the trace is walked.
        self._decision_alternatives: list[str] = list(ALTERNATIVES)

    # ------------------------------------------------------------------ events

    def _emit(self, kind: str, **detail) -> None:
        if self.on_event:
            self.on_event({"kind": kind, "mode": self.mode, "ts": time.time(), **detail})

    def _pause(self) -> None:
        if self.step_pause_s:
            time.sleep(self.step_pause_s)

    # ------------------------------------------------------------------ lease

    def acquire(self, workflow_id: str) -> int:
        """Leadership is a lease with a monotonically increasing epoch."""
        if not self.renew_lease:
            return self.epoch
        self.epoch = self.j.acquire_lease(workflow_id, self.owner, self.lease_ttl_s)
        return self.epoch

    # ---------------------------------------------------------------- barrier

    def barrier_check(self, workflow_id: str) -> tuple[bool, list]:
        """A branch may not execute an irreversible effect while ANY abandoned branch
        in the same workflow holds uncompensated compensatable effects (2.2)."""
        pending = self.j.uncompensated(workflow_id)
        return (not pending, pending)

    def compensate_abandoned(self, workflow_id: str) -> bool:
        """Bounded compensation driver."""
        deadline = time.time() + self.barrier_deadline_s

        for attempt in range(self.max_comp_attempts):
            pending = self.j.uncompensated(workflow_id)
            if not pending:
                return True

            # Reverse execution order, LIFO (2.6). Ordered by record_id, not by seq:
            # several abandoned branches can share a seq, but never a record_id.
            for rec in sorted(pending, key=lambda r: r.record_id, reverse=True):
                if time.time() > deadline:
                    self._emit("compensation_deadline", attempt=attempt + 1)
                    return False

                self.j.append(
                    workflow_id, rec.branch_id, rec.seq, "COMP_INTENT",
                    tool_name=rec.tool_name, effect_type=rec.effect_type,
                    args=rec.args, key=rec.key, epoch=self.epoch,
                    detail={"attempt": attempt + 1},
                )
                res = self.world.compensate(
                    rec.tool_name, rec.args or {}, rec.key, self.epoch, self.timeout_s
                )
                self.j.append(
                    workflow_id, rec.branch_id, rec.seq, "COMP_RESULT",
                    tool_name=rec.tool_name, effect_type=rec.effect_type,
                    args=rec.args, key=rec.key, epoch=self.epoch, result=res,
                    detail={"attempt": attempt + 1},
                )
                self._emit(
                    "compensated" if res.status == "ok" else "compensation_failed",
                    tool=rec.tool_name, attempt=attempt + 1, error=res.error,
                    remaining=len(self.j.uncompensated(workflow_id)),
                )
                self._pause()

            if not self.j.uncompensated(workflow_id):
                return True

            remaining = deadline - time.time()
            if remaining <= 0:
                return False
            time.sleep(min(COMP_BACKOFF_S * (2**attempt), remaining))

        return not self.j.uncompensated(workflow_id)

    def _barrier(self, workflow_id: str, branch, seq: int, tool: str, et, key: str) -> None:
        ok, pending = self.barrier_check(workflow_id)
        if ok:
            return

        reason = f"{len(pending)} uncompensated effects on abandoned branch"
        self.j.append(
            workflow_id, branch.branch_id, seq, "BARRIER_BLOCKED",
            tool_name=tool, effect_type=et, key=key, epoch=self.epoch,
            detail={
                "reason": reason,
                "count": len(pending),
                "effects": [p.tool_name for p in pending],
                "branches": sorted({p.branch_id for p in pending}),
                "blocked_tool": tool,
            },
        )
        self._emit("barrier_blocked", count=len(pending),
                   effects=[p.tool_name for p in pending], reason=reason)
        self._pause()

        if not self.compensate_abandoned(workflow_id):
            still = self.j.uncompensated(workflow_id)
            raise Escalation(
                "compensation exhausted, barrier not satisfied",
                {
                    "blocked_tool": tool,
                    "uncompensated": [
                        {"tool": p.tool_name, "key": p.key, "branch_id": p.branch_id}
                        for p in still
                    ],
                    "attempts": self.max_comp_attempts,
                    "deadline_s": self.barrier_deadline_s,
                },
            )

        # Abandoned branches that are now fully drained become "compensated".
        for b in self.j.branches(workflow_id):
            if b.status == "abandoned":
                self.j.set_branch_status(b.branch_id, "compensated")
                self.j.append(
                    workflow_id, b.branch_id, self.j.next_seq(b.branch_id),
                    "BRANCH_COMPENSATED", detail={"depth": b.depth},
                )

        self.j.append(
            workflow_id, branch.branch_id, seq, "BARRIER_RELEASED",
            tool_name=tool, effect_type=et, key=key, epoch=self.epoch,
            detail={"count": 0, "released_tool": tool},
        )
        self._emit("barrier_released", tool=tool)
        self._pause()

    # ------------------------------------------------------------- branch tree

    def _abandon(self, workflow_id: str, branch_id: str) -> str:
        """Abandon a branch."""
        # Mark it dead FIRST. journal.residue() only reports effects on abandoned branches.
        self.j.set_branch_status(branch_id, "abandoned")
        residue = [r for r in self.j.residue(workflow_id) if r.branch_id == branch_id]
        status = "abandoned_with_residue" if residue else "abandoned"
        if status != "abandoned":
            self.j.set_branch_status(branch_id, status)
        self.j.append(
            workflow_id, branch_id, self.j.next_seq(branch_id), "BRANCH_ABANDONED",
            detail={
                "status": status,
                "residue": [
                    {"tool": r.tool_name, "args": r.args, "record_id": r.record_id}
                    for r in residue
                ],
                "will_compensate": self.caps["compensates"],
            },
        )
        self._emit("branch_abandoned", branch_id=branch_id, status=status,
                   residue=[r.tool_name for r in residue])
        return status

    def _tried_alternatives(self, workflow_id: str) -> set[str]:
        """Exclude anything already tried on an abandoned ancestor (2.5)."""
        out = set()
        for r in self.j.records(workflow_id):
            if r.kind == "RESULT" and r.tool_name == "classify" and r.args:
                if r.result and r.result.status == "ok":
                    out.add(r.args.get("severity"))
            if r.kind == "BRANCH_FORKED" and r.detail:
                out.add(r.detail.get("new_severity"))
        return {s for s in out if s}

    def _severity_for_branch(self, branch_id: str, fallback: str) -> str:
        """Recover the classification from the journal, not from the caller."""
        sev = None
        for r in self.j.branch_records(branch_id):
            if r.kind == "BRANCH_FORKED" and r.detail and r.detail.get("new_severity"):
                sev = r.detail["new_severity"]
            if (
                r.kind == "RESULT"
                and r.tool_name == "classify"
                and r.args
                and r.result
                and r.result.status == "ok"
            ):
                sev = r.args.get("severity") or sev
        return sev or fallback

    def _supersede_for(self, workflow_id: str) -> dict | None:
        residue = self.j.residue(workflow_id)
        pages = [r for r in residue if r.tool_name == "page_oncall"]
        return supersede_annotation(pages[-1]) if pages else None

    def _escalation_payload(self, workflow_id: str) -> dict:
        """The escalation record carries the full branch tree and the uncompensated
        effect list, so a human inherits the whole account rather than a bare alarm."""
        return {
            "branch_tree": [b.to_dict() for b in self.j.branches(workflow_id)],
            "uncompensated": [
                {"tool": r.tool_name, "key": r.key, "branch_id": r.branch_id}
                for r in self.j.uncompensated(workflow_id)
            ],
            "residue": [
                {"tool": r.tool_name, "args": r.args, "branch_id": r.branch_id}
                for r in self.j.residue(workflow_id)
            ],
        }

    def _select_branch(self, workflow_id: str):
        if not self.caps["replays"]:
            # Option B: the fresh run does not know what already happened.
            prior = self.j.active_branch(workflow_id)
            if prior is not None:
                self._abandon(workflow_id, prior.branch_id)
            return self.j.create_branch(workflow_id)
        return self.j.active_branch(workflow_id) or self.j.create_branch(workflow_id)

    # -------------------------------------------------------------------- run

    def run(self, alert: Alert, severity: str = "P2") -> dict:
        workflow_id = workflow_id_for(alert.alert_id)
        try:
            self.acquire(workflow_id)
        except LeaseUnavailable as e:
            return {"outcome": "not_leader", "reason": str(e), "workflow_id": workflow_id}

        branch = self._select_branch(workflow_id)
        severity = self._severity_for_branch(branch.branch_id, severity)

        while True:
            try:
                return self._run_branch(workflow_id, branch, alert, severity)

            except Escalation as e:
                self.j.append(
                    workflow_id, branch.branch_id, self.j.next_seq(branch.branch_id),
                    "ESCALATED",
                    detail={
                        "reason": e.reason,
                        **e.detail,
                        **self._escalation_payload(workflow_id),
                    },
                )
                self._emit("escalated", reason=e.reason)
                return {
                    "outcome": "escalated",
                    "reason": e.reason,
                    "detail": e.detail,
                    "workflow_id": workflow_id,
                    "branch_id": branch.branch_id,
                    "severity": severity,
                }

            except _Diverge as d:
                if not self.caps["diverges"]:
                    # Pinned to the journaled decision.
                    self._emit("replay_failed", reason=d.reason)
                    return {
                        "outcome": "replay_failed",
                        "reason": d.reason,
                        "workflow_id": workflow_id,
                        "branch_id": branch.branch_id,
                        "severity": severity,
                    }

                if branch.depth + 1 > self.max_fork_depth:
                    return self._escalate_now(
                        workflow_id, branch,
                        f"fork bound reached at depth {self.max_fork_depth}",
                        {"depth": branch.depth, "max_fork_depth": self.max_fork_depth},
                        severity,
                    )

                tried = self._tried_alternatives(workflow_id)
                ranked = self._decision_alternatives or list(ALTERNATIVES)
                alternatives = [a for a in ranked if a not in tried]
                if not alternatives:
                    return self._escalate_now(
                        workflow_id, branch, "alternatives exhausted",
                        {"tried": sorted(tried), "ranked": list(ranked)}, severity,
                    )

                chosen = alternatives[0]
                self._abandon(workflow_id, branch.branch_id)
                new_branch = self.j.create_branch(
                    workflow_id, branch.branch_id, d.fork_point_record_id, branch.depth + 1
                )
                self.j.append(
                    workflow_id, new_branch.branch_id, 0, "BRANCH_FORKED",
                    detail={
                        "parent": branch.branch_id,
                        "fork_point": d.fork_point_record_id,
                        "reason": d.reason,
                        "abandoned_severity": severity,
                        "new_severity": chosen,
                        "depth": new_branch.depth,
                        "max_fork_depth": self.max_fork_depth,
                    },
                )
                self._emit("forked", parent=branch.branch_id, branch_id=new_branch.branch_id,
                           depth=new_branch.depth, severity=chosen, reason=d.reason)
                self._pause()
                branch, severity = new_branch, chosen

    def _escalate_now(self, workflow_id, branch, reason: str, detail: dict, severity: str) -> dict:
        self.j.append(
            workflow_id, branch.branch_id, self.j.next_seq(branch.branch_id), "ESCALATED",
            detail={"reason": reason, **detail, **self._escalation_payload(workflow_id)},
        )
        self._emit("escalated", reason=reason)
        return {
            "outcome": "escalated",
            "reason": reason,
            "detail": detail,
            "workflow_id": workflow_id,
            "branch_id": branch.branch_id,
            "severity": severity,
        }

    # ------------------------------------------------------------ step machine

    def _run_branch(self, workflow_id: str, branch, alert: Alert, severity: str) -> dict:
        supersede = self._supersede_for(workflow_id) if self.caps["barrier"] else None
        steps = trace_for(alert, severity, supersede=supersede)
        done = self.j.completed_results(branch.branch_id) if self.caps["replays"] else {}
        classify_record_id = None

        for seq, step in enumerate(steps):
            tool = step["tool"]
            et = EFFECT_TYPES[tool]

            # Read alternatives before the replay skip; a resumed branch still needs them.
            if step.get("decision") and step.get("alternatives"):
                self._decision_alternatives = list(step["alternatives"])

            if seq in done:
                if seq == CLASSIFY_SEQ:
                    classify_record_id = done[seq].record_id
                self._emit("step_replayed", seq=seq, tool=tool)
                continue

            key = effect_key(workflow_id, branch.branch_id, seq)
            prior = self.j.last_result(branch.branch_id, seq) if self.caps["replays"] else None

            if self.crash:
                self.crash.check(seq, "before_intent")

            if et.reversibility == "irreversible" and self.caps["barrier"]:
                self._barrier(workflow_id, branch, seq, tool, et, key)

            self.j.append(
                workflow_id, branch.branch_id, seq, "INTENT",
                tool_name=tool, effect_type=et, args=step["args"], key=key, epoch=self.epoch,
            )
            self._emit("intent", seq=seq, tool=tool, args=step["args"])

            if self.crash:
                self.crash.check(seq, "after_intent")

            res = self.world.execute(tool, step["args"], key, self.epoch, self.timeout_s)

            if self.crash:
                # The nastiest boundary: the effect committed but no result is journaled.
                self.crash.check(seq, "after_effect")

            res = self._resolve_unknown(workflow_id, branch, seq, tool, et, step["args"], key, res)

            if (
                prior is not None
                and prior.result is not None
                and prior.result.status == "unknown"
                and res.status == "ok"
            ):
                # We gave up on this as unknown, but the key says it landed. Caught, not re-issued.
                self.j.append(
                    workflow_id, branch.branch_id, seq, "LATE_DELIVERY_SUPPRESSED",
                    tool_name=tool, effect_type=et, args=step["args"], key=key,
                    epoch=self.epoch, result=res,
                    detail={
                        "prior_record_id": prior.record_id,
                        "late": bool((res.value or {}).get("late")),
                        "note": "effect already committed under this idempotency key;"
                                " not re-issued",
                    },
                )
                self._emit("late_delivery_suppressed", seq=seq, tool=tool, key=key)

            result_rid = self.j.append(
                workflow_id, branch.branch_id, seq, "RESULT",
                tool_name=tool, effect_type=et, args=step["args"], key=key,
                epoch=self.epoch, result=res,
            )
            self._emit("result", seq=seq, tool=tool, status=res.status, error=res.error)

            if seq == CLASSIFY_SEQ:
                classify_record_id = result_rid

            if self.crash:
                self.crash.check(seq, "after_result")

            if res.status == "failed":
                if et.reversibility == "irreversible":
                    # The irreversible step is the one worth diverging over: a different
                    # classification routes to a different rota.
                    raise _Diverge(res.error or f"{tool} failed", classify_record_id)
                raise Escalation(f"{tool} failed: {res.error}", {"tool": tool, "seq": seq})

            self._pause()

            if (
                self._diverge_armed
                and self.diverge_after_seq is not None
                and seq == self.diverge_after_seq
            ):
                self._diverge_armed = False
                raise _Diverge(self.diverge_reason, classify_record_id)

        return {
            "outcome": "completed",
            "severity": severity,
            "workflow_id": workflow_id,
            "branch_id": branch.branch_id,
            "rota": steps[PAGE_SEQ]["args"].get("rota"),
        }

    def _resolve_unknown(self, workflow_id, branch, seq, tool, et, args, key, res) -> ToolResult:
        """A timeout is not a failure."""
        if res.status != "unknown":
            return res

        probe = self.world.probe(tool, key, self.timeout_s)
        self._emit("probed", seq=seq, tool=tool, probe=probe)

        if probe == "done":
            return ToolResult("ok", {"probed": "done", "key": key}, None)
        if probe == "not_done":
            return ToolResult("failed", error=f"{tool} timed out; probe says not_done")

        # Unknown and unprobeable.
        self.j.append(
            workflow_id, branch.branch_id, seq, "RESULT",
            tool_name=tool, effect_type=et, args=args, key=key,
            epoch=self.epoch, result=res, detail={"probe": probe, "bounded_ambiguity": True},
        )
        raise Escalation(
            f"{tool} in unknown state, bounded ambiguity surfaced",
            {
                "tool": tool,
                "seq": seq,
                "key": key,
                "probe": probe,
                "effect_type": et.to_dict(),
            },
        )

    # ---------------------------------------------------------- reconciliation

    def reconcile_unknowns(self, workflow_id: str) -> list[dict]:
        """Re-drive every effect left in unknown state through its idempotency key."""
        out: list[dict] = []
        latest: dict[tuple[str, int], object] = {}
        for r in self.j.records(workflow_id):
            if r.kind == "RESULT":
                latest[(r.branch_id, r.seq)] = r

        for (branch_id, seq), rec in sorted(latest.items(), key=lambda kv: kv[1].record_id):
            if not rec.result or rec.result.status != "unknown":
                continue

            res = self.world.execute(
                rec.tool_name, rec.args or {}, rec.key, self.epoch, self.timeout_s
            )
            if res.status == "unknown":
                probe = self.world.probe(rec.tool_name, rec.key, self.timeout_s)
                if probe == "done":
                    res = ToolResult("ok", {"probed": "done", "key": rec.key}, None)

            if res.status == "ok":
                self.j.append(
                    workflow_id, branch_id, seq, "LATE_DELIVERY_SUPPRESSED",
                    tool_name=rec.tool_name, effect_type=rec.effect_type, args=rec.args,
                    key=rec.key, epoch=self.epoch, result=res,
                    detail={
                        "prior_record_id": rec.record_id,
                        "late": bool((res.value or {}).get("late")),
                        "note": "effect landed after we surfaced it as unknown;"
                                " caught on the idempotency key, not re-issued",
                    },
                )
                self.j.append(
                    workflow_id, branch_id, seq, "RESULT",
                    tool_name=rec.tool_name, effect_type=rec.effect_type, args=rec.args,
                    key=rec.key, epoch=self.epoch, result=res,
                    detail={"reconciled": True},
                )
                self._emit("late_delivery_suppressed", seq=seq, tool=rec.tool_name, key=rec.key)
                out.append({"tool": rec.tool_name, "key": rec.key, "resolved": "committed"})
            else:
                out.append({"tool": rec.tool_name, "key": rec.key, "resolved": "still_unknown"})

        return out


def recover(
    journal: Journal,
    world: World,
    alert: Alert,
    severity: str = "P2",
    mode: str = "sluice",
    owner: str = "orch-a",
    max_attempts: int = 6,
    **kwargs,
) -> dict:
    """Drive a workflow to quiescence across process crashes."""
    attempts = 0
    last: dict = {"outcome": "livelocked", "workflow_id": workflow_id_for(alert.alert_id)}
    while attempts < max_attempts:
        attempts += 1
        orch = Orchestrator(journal, world, owner=owner, mode=mode, **kwargs)
        try:
            last = orch.run(alert, severity)
        except ProcessCrash as e:
            last = {
                "outcome": "crashed",
                "reason": str(e),
                "workflow_id": workflow_id_for(alert.alert_id),
            }
            kwargs.pop("crash", None)  # the injected crash fires once
            continue

        if last.get("outcome") in ("completed", "escalated", "not_leader"):
            last["attempts"] = attempts
            return last

        # replay_failed: pinned replay retries the same journaled decision forever.
        kwargs.pop("crash", None)

    last["attempts"] = attempts
    if last.get("outcome") in ("replay_failed", "crashed"):
        # Never reached a terminal state within the attempt budget.
        last = {**last, "outcome": "livelocked", "replay_reason": last.get("reason")}
    return last
