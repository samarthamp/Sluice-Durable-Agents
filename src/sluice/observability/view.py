"""Journal -> dashboard state."""

from __future__ import annotations

import time

from ..core.journal import Journal
from ..core.tools import COMPENSATIONS, ROTA_FOR_SEVERITY, STEP_LABELS
from ..core.types import DEAD_BRANCH_STATUSES, JournalRecord

BARRIER_LABELS = {
    "idle": "no irreversible effect pending",
    "blocked": "irreversible effect blocked",
    "released": "cleanup proven, gate lifted",
    "escalated": "compensation failed: escalated to human, incident not lost",
}


def _latest_results(records: list[JournalRecord]) -> dict[tuple[str, int], JournalRecord]:
    out: dict[tuple[str, int], JournalRecord] = {}
    for r in records:
        if r.kind == "RESULT":
            out[(r.branch_id, r.seq)] = r
    return out


def _branch_severity(records: list[JournalRecord], branch_id: str) -> str | None:
    sev = None
    for r in records:
        if r.branch_id != branch_id:
            continue
        if r.kind == "BRANCH_FORKED" and r.detail and r.detail.get("new_severity"):
            sev = r.detail["new_severity"]
        if r.kind == "RESULT" and r.tool_name == "classify" and r.args:
            sev = r.args.get("severity") or sev
    return sev


def scoreboard(records: list[JournalRecord]) -> dict:
    """Net effect counts, journal-derived, deduplicated by idempotency key."""
    committed: dict[str, str] = {}
    compensated: set[str] = set()
    for r in records:
        if not r.key:
            continue
        if r.kind == "RESULT" and r.result and r.result.status == "ok" and r.tool_name:
            committed[r.key] = r.tool_name
        if r.kind == "COMP_RESULT" and r.result and r.result.status == "ok":
            compensated.add(r.key)

    net: dict[str, int] = {}
    gross: dict[str, int] = {}
    for key, tool in committed.items():
        gross[tool] = gross.get(tool, 0) + 1
        if key not in compensated:
            net[tool] = net.get(tool, 0) + 1

    return {
        "tickets": net.get("create_ticket", 0),
        "posts": net.get("post_to_channel", 0),
        "pages": net.get("page_oncall", 0),
        "net": net,
        "gross": gross,
        "compensated": len(compensated),
    }


def barrier_state(records: list[JournalRecord]) -> dict:
    """Blocked / lifted / escalated -- the gate's three still frames (6.1)."""
    state = "idle"
    label = BARRIER_LABELS["idle"]
    count = 0
    effects: list[str] = []

    for r in records:
        if r.kind == "BARRIER_BLOCKED":
            d = r.detail or {}
            state = "blocked"
            count = d.get("count", 0)
            effects = list(d.get("effects", []))
            # The gate label comes from the record's detail.
            label = f"irreversible effect blocked: {d.get('reason', '')}"
        elif r.kind == "BARRIER_RELEASED":
            state = "released"
            count = 0
            effects = []
            label = BARRIER_LABELS["released"]
        elif r.kind == "COMP_RESULT" and state == "blocked":
            if r.result and r.result.status == "ok" and count:
                count -= 1
                if r.tool_name in effects:
                    effects.remove(r.tool_name)
                label = f"irreversible effect blocked: {count} uncompensated effects remain"
        elif r.kind == "ESCALATED" and state == "blocked":
            state = "escalated"
            label = BARRIER_LABELS["escalated"]

    return {"state": state, "label": label, "count": count, "effects": effects}


def phones(records: list[JournalRecord]) -> list[dict]:
    """One entry per page that actually committed (6.2)."""
    out: list[dict] = []
    seen: set[str] = set()
    for r in records:
        if r.kind != "RESULT" or r.tool_name != "page_oncall":
            continue
        if not r.result or r.result.status != "ok" or not r.key or r.key in seen:
            continue
        seen.add(r.key)
        args = r.args or {}
        out.append(
            {
                "rota": args.get("rota"),
                "severity": args.get("severity"),
                "incident": args.get("incident"),
                "supersedes": args.get("supersedes"),
                "ts": r.ts,
                "branch_id": r.branch_id,
            }
        )
    return out


def branch_view(journal: Journal, workflow_id: str, records: list[JournalRecord]) -> list[dict]:
    latest = _latest_results(records)
    intents = {(r.branch_id, r.seq) for r in records if r.kind == "INTENT"}
    comped_keys = {
        r.key for r in records if r.kind == "COMP_RESULT" and r.result and r.result.status == "ok"
    }

    out = []
    for b in journal.branches(workflow_id):
        steps = []
        for seq, tool in enumerate(STEP_LABELS):
            rec = latest.get((b.branch_id, seq))
            if rec is None:
                status = "in_flight" if (b.branch_id, seq) in intents else "pending"
            elif rec.result is None:
                status = "in_flight"
            elif rec.key in comped_keys:
                status = "compensated"
            else:
                status = rec.result.status
            steps.append(
                {
                    "seq": seq,
                    "tool": tool,
                    "status": status,
                    "compensatable": tool in COMPENSATIONS,
                    "irreversible": tool == "page_oncall",
                    "ts": rec.ts if rec else None,
                    "error": rec.result.error if rec and rec.result else None,
                }
            )
        sev = _branch_severity(records, b.branch_id)
        out.append(
            {
                **b.to_dict(),
                "severity": sev,
                "rota": ROTA_FOR_SEVERITY.get(sev or "", None),
                "dead": b.status in DEAD_BRANCH_STATUSES,
                "steps": steps,
            }
        )
    return out


def derive_state(journal: Journal, workflow_id: str, mode: str = "") -> dict:
    records = journal.records(workflow_id)
    if not records:
        return {"workflow_id": workflow_id, "empty": True, "now": time.time()}

    uncompensated = journal.uncompensated(workflow_id)
    residue = journal.residue(workflow_id)
    lease = journal.lease_info(workflow_id)

    escalations = [
        {"ts": r.ts, "reason": (r.detail or {}).get("reason"), "detail": r.detail}
        for r in records
        if r.kind == "ESCALATED"
    ]
    late = [
        {"ts": r.ts, "tool": r.tool_name, "key": r.key, "detail": r.detail}
        for r in records
        if r.kind == "LATE_DELIVERY_SUPPRESSED"
    ]

    board = scoreboard(records)
    started = records[0].ts
    last = records[-1]

    # The incident timer.
    terminal = bool(board["pages"]) or bool(escalations)
    elapsed = (last.ts if terminal else time.time()) - started

    return {
        "workflow_id": workflow_id,
        "mode": mode,
        "empty": False,
        "now": time.time(),
        "started_ts": started,
        "elapsed_s": elapsed,
        "running": not terminal,
        "last_record_id": last.record_id,
        "branches": branch_view(journal, workflow_id, records),
        "barrier": barrier_state(records),
        "scoreboard": board,
        "phones": phones(records),
        "uncompensated": [
            {"tool": r.tool_name, "key": r.key, "branch_id": r.branch_id, "seq": r.seq}
            for r in uncompensated
        ],
        "residue": [
            {"tool": r.tool_name, "args": r.args, "branch_id": r.branch_id} for r in residue
        ],
        "escalations": escalations,
        "late_deliveries": late,
        "lease": {
            "owner": lease.owner,
            "epoch": lease.epoch,
            "expires_in": max(0.0, lease.expires - time.time()),
        }
        if lease
        else None,
        "events": [
            {
                "record_id": r.record_id,
                "ts": r.ts,
                "kind": r.kind,
                "branch_id": r.branch_id,
                "seq": r.seq,
                "tool": r.tool_name,
                "status": r.result.status if r.result else None,
                "detail": r.detail,
            }
            for r in records
            if r.kind
            in (
                "BRANCH_FORKED",
                "BRANCH_ABANDONED",
                "BRANCH_COMPENSATED",
                "BARRIER_BLOCKED",
                "BARRIER_RELEASED",
                "ESCALATED",
                "LATE_DELIVERY_SUPPRESSED",
                "COMP_RESULT",
            )
        ],
    }


def render_terminal(state: dict) -> str:
    """The fallback demo surface.  If the UI dies, this is what you show (6.6)."""
    if state.get("empty"):
        return f"{state['workflow_id']}: no records"

    b = state["barrier"]
    s = state["scoreboard"]
    lines = [
        f"workflow {state['workflow_id']}  mode={state['mode'] or '?'}"
        f"  elapsed {state['elapsed_s']:.1f}s",
        f"  gate: [{b['state'].upper()}] {b['label']}",
        f"  scoreboard: tickets {s['tickets']}  posts {s['posts']}  pages {s['pages']}"
        f"   (gross {s['gross'].get('create_ticket', 0)}/"
        f"{s['gross'].get('post_to_channel', 0)}/{s['gross'].get('page_oncall', 0)})",
    ]
    for br in state["branches"]:
        marks = {
            "ok": "#",
            "failed": "x",
            "unknown": "?",
            "compensated": "~",
            "in_flight": ">",
            "pending": ".",
        }
        path = "".join(marks.get(st["status"], ".") for st in br["steps"])
        lines.append(
            f"  {br['branch_id']}  d{br['depth']}  {br['status']:<24}"
            f" {br['severity'] or '--'}  {path}"
        )
    for p in state["phones"]:
        tag = "  (supersedes " + str(p["supersedes"].get("rota")) + ")" if p["supersedes"] else ""
        lines.append(f"  phone: {p['rota']} rang{tag}")
    for e in state["escalations"]:
        lines.append(f"  escalated: {e['reason']}")
    for ld in state["late_deliveries"]:
        lines.append(f"  late delivery suppressed: {ld['tool']} on key {ld['key']}")
    return "\n".join(lines)
