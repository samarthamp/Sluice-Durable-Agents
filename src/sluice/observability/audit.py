"""Audit export: the branch tree rendered as a readable incident post-mortem (5.7)."""

from __future__ import annotations

import time

from ..core.journal import Journal
from ..core.tools import STEP_LABELS
from .view import derive_state

_STATUS_NOTE = {
    "active": "the branch that ran to completion",
    "abandoned": "abandoned, cleanup not yet proven",
    "compensated": "abandoned and fully compensated in reverse order",
    "abandoned_with_residue": "abandoned carrying a permanent irreversible effect",
}


def _ts(t: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(t)) + f".{int((t % 1) * 1000):03d}"


def post_mortem(journal: Journal, workflow_id: str, mode: str = "") -> str:
    recs = journal.records(workflow_id)
    if not recs:
        return f"# Incident post-mortem\n\nNo records for {workflow_id}.\n"

    state = derive_state(journal, workflow_id, mode=mode)
    t0 = recs[0].ts
    board = state["scoreboard"]

    out: list[str] = [
        f"# Incident post-mortem: {workflow_id}",
        "",
        f"- mode: `{mode or 'unknown'}`",
        f"- opened: {_ts(t0)}",
        f"- duration: {state['elapsed_s']:.2f}s",
        f"- terminal state: {'running' if state['running'] else 'quiesced'}",
        "",
        "## Effects standing at the end",
        "",
        "| effect | net | gross | note |",
        "| --- | --- | --- | --- |",
    ]
    for tool in ("create_ticket", "post_to_channel", "page_oncall", "update_status_page"):
        net = board["net"].get(tool, 0)
        gross = board["gross"].get(tool, 0)
        note = "compensated on an abandoned branch" if gross > net else ""
        out.append(f"| `{tool}` | {net} | {gross} | {note} |")

    out += ["", "## Branch tree", ""]
    for b in state["branches"]:
        note = _STATUS_NOTE.get(b["status"], "")
        parent = f" forked from `{b['parent_branch_id']}`" if b["parent_branch_id"] else ""
        out.append(
            f"### `{b['branch_id']}` — depth {b['depth']}, **{b['status']}**{parent}"
        )
        out.append("")
        out.append(f"Classification `{b['severity'] or '--'}` routing to `{b['rota'] or '--'}`."
                   f" {note}.")
        out.append("")
        out.append("| step | tool | outcome |")
        out.append("| --- | --- | --- |")
        for st in b["steps"]:
            if st["status"] == "pending":
                continue
            detail = f" — {st['error']}" if st["error"] else ""
            out.append(f"| {st['seq']} | `{st['tool']}` | {st['status']}{detail} |")
        out.append("")

    comps = [r for r in recs if r.kind == "COMP_RESULT"]
    if comps:
        out += [
            "## Compensation",
            "",
            "Reverse execution order, LIFO, as sagas specify (Garcia-Molina and Salem,"
            " 1987). The channel post is deleted before the ticket it references is"
            " closed, so no live post is left pointing at a closed ticket.",
            "",
            "| time | attempt | undo | result |",
            "| --- | --- | --- | --- |",
        ]
        for r in comps:
            status = r.result.status if r.result else "?"
            err = f" — {r.result.error}" if r.result and r.result.error else ""
            out.append(
                f"| {_ts(r.ts)} | {(r.detail or {}).get('attempt', 1)} |"
                f" `{r.tool_name}` | {status}{err} |"
            )
        out.append("")

    if state["residue"]:
        out += [
            "## Irreversible residue",
            "",
            "Compensation restores state, not history.  These effects were committed"
            " on a branch that was later abandoned.  They cannot be undone, so they"
            " are recorded and superseded rather than silently erased.",
            "",
        ]
        for r in state["residue"]:
            args = r["args"] or {}
            out.append(
                f"- `{r['tool']}` to **{args.get('rota', '?')}** under classification"
                f" `{args.get('severity', '?')}` on branch `{r['branch_id']}`"
            )
        out.append("")

    supers = [p for p in state["phones"] if p.get("supersedes")]
    if supers:
        out += ["## Supersede annotations", ""]
        for p in supers:
            out.append(f"- page to **{p['rota']}**: {p['supersedes'].get('note')}")
        out.append("")

    if state["late_deliveries"]:
        out += [
            "## Late-arriving effects",
            "",
            "An effect we had surfaced as unknown landed afterwards.  It was caught on"
            " the idempotency key and not re-issued.",
            "",
        ]
        for ld in state["late_deliveries"]:
            note = (ld["detail"] or {}).get("note")
            out.append(f"- `{ld['tool']}` on key `{ld['key']}` — {note}")
        out.append("")

    if state["escalations"]:
        out += [
            "## Escalations",
            "",
            "A workflow that escalates has not lost the signal.  It has routed it to a"
            " human instead of a rota, which is a degraded but honest outcome.",
            "",
        ]
        for e in state["escalations"]:
            out.append(f"- **{e['reason']}**")
            for item in (e["detail"] or {}).get("uncompensated", []) or []:
                out.append(f"  - uncompensated: `{item.get('tool')}` key `{item.get('key')}`")
        out.append("")

    if state["uncompensated"]:
        out += ["## Still uncompensated at quiescence", ""]
        for u in state["uncompensated"]:
            out.append(f"- `{u['tool']}` on branch `{u['branch_id']}` (key `{u['key']}`)")
        out.append("")

    out += ["## Full journal", "", "| # | time | branch | kind | tool | detail |",
            "| --- | --- | --- | --- | --- | --- |"]
    for r in recs:
        detail = ""
        if r.kind in ("INTENT", "RESULT") and r.tool_name:
            detail = r.result.status if r.result else "issued"
            if r.result and r.result.error:
                detail += f" ({r.result.error})"
        elif r.detail:
            detail = (r.detail.get("reason") or r.detail.get("status") or "")[:120]
        label = STEP_LABELS[r.seq] if r.tool_name is None and 0 <= r.seq < len(STEP_LABELS) else ""
        out.append(
            f"| {r.record_id} | {_ts(r.ts)} | `{r.branch_id}` | {r.kind} |"
            f" `{r.tool_name or label or '-'}` | {detail} |"
        )

    out.append("")
    return "\n".join(out)


def write_post_mortem(journal: Journal, workflow_id: str, path: str, mode: str = "") -> str:
    text = post_mortem(journal, workflow_id, mode=mode)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path
