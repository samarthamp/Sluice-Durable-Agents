"""The journal is a tree, not a log (2.1)."""

from __future__ import annotations

import json
import pathlib
import sqlite3
import threading
import time
import uuid

from .types import (
    DEAD_BRANCH_STATUSES,
    Branch,
    BranchStatus,
    EffectType,
    JournalRecord,
    LeaseInfo,
    RecordKind,
    ToolResult,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    record_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    workflow_id TEXT NOT NULL,
    branch_id   TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    tool_name   TEXT,
    effect_type TEXT,
    args        TEXT,
    key         TEXT,
    epoch       INTEGER,
    result      TEXT,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_records_wf ON records(workflow_id);
CREATE INDEX IF NOT EXISTS idx_records_branch ON records(branch_id, seq);
CREATE INDEX IF NOT EXISTS idx_records_key ON records(key);

CREATE TABLE IF NOT EXISTS branches (
    branch_id            TEXT PRIMARY KEY,
    workflow_id          TEXT NOT NULL,
    parent_branch_id     TEXT,
    fork_point_record_id INTEGER,
    depth                INTEGER NOT NULL,
    status               TEXT NOT NULL,
    created_ts           REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_branches_wf ON branches(workflow_id);

CREATE TABLE IF NOT EXISTS leases (
    scope   TEXT PRIMARY KEY,
    owner   TEXT NOT NULL,
    epoch   INTEGER NOT NULL,
    expires REAL NOT NULL
);
"""

_COLUMNS = (
    "record_id, ts, workflow_id, branch_id, seq, kind, tool_name,"
    " effect_type, args, key, epoch, result, detail"
)


class LeaseUnavailable(RuntimeError):
    """Another orchestrator holds an unexpired lease on this scope."""


def _dumps(x):
    return None if x is None else json.dumps(x)


def _loads(x):
    return None if x is None else json.loads(x)


class Journal:
    SYNCHRONOUS_LEVELS = ("FULL", "NORMAL", "OFF")

    def __init__(
        self,
        path: str = "sluice.db",
        read_only: bool = False,
        synchronous: str = "FULL",
    ):
        self.path = path
        self.read_only = read_only
        synchronous = synchronous.upper()
        if synchronous not in self.SYNCHRONOUS_LEVELS:
            raise ValueError(f"synchronous must be one of {self.SYNCHRONOUS_LEVELS}")
        self.synchronous = synchronous

        if read_only:
            # as_uri() gives the canonical form on every OS (file:///C:/... on Windows)
            # and percent-encodes characters such as ? and # that would end the path.
            uri = pathlib.Path(path).resolve().as_uri() + "?mode=ro"
            self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        else:
            self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
            self.conn.execute("PRAGMA journal_mode=WAL")
            # FULL, not NORMAL: losing the intent record makes recovery blind.
            self.conn.execute(f"PRAGMA synchronous={synchronous}")
            self.conn.executescript(SCHEMA)
        self.conn.execute("PRAGMA busy_timeout=5000")
        # BEGIN IMMEDIATE serialises lease acquisition across processes, but not across
        # threads sharing this one connection; this lock covers that case.
        self._tx_lock = threading.Lock()

    def close(self) -> None:
        self.conn.close()

    # ---------------------------------------------------------------- records

    def append(
        self,
        workflow_id: str,
        branch_id: str,
        seq: int,
        kind: RecordKind,
        tool_name: str | None = None,
        effect_type: EffectType | None = None,
        args: dict | None = None,
        key: str | None = None,
        epoch: int | None = None,
        result: ToolResult | None = None,
        detail: dict | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO records (ts, workflow_id, branch_id, seq, kind, tool_name,"
            " effect_type, args, key, epoch, result, detail)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                time.time(),
                workflow_id,
                branch_id,
                seq,
                kind,
                tool_name,
                _dumps(effect_type.to_dict() if effect_type else None),
                _dumps(args),
                key,
                epoch,
                _dumps(result.to_dict() if result else None),
                _dumps(detail),
            ),
        )
        return cur.lastrowid

    def _row_to_record(self, r) -> JournalRecord:
        return JournalRecord(
            record_id=r[0],
            ts=r[1],
            workflow_id=r[2],
            branch_id=r[3],
            seq=r[4],
            kind=r[5],
            tool_name=r[6],
            effect_type=EffectType.from_dict(_loads(r[7])),
            args=_loads(r[8]),
            key=r[9],
            epoch=r[10],
            result=ToolResult.from_dict(_loads(r[11])),
            detail=_loads(r[12]),
        )

    def records(self, workflow_id: str) -> list[JournalRecord]:
        rows = self.conn.execute(
            f"SELECT {_COLUMNS} FROM records WHERE workflow_id=? ORDER BY record_id",
            (workflow_id,),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def branch_records(self, branch_id: str) -> list[JournalRecord]:
        rows = self.conn.execute(
            f"SELECT {_COLUMNS} FROM records WHERE branch_id=? ORDER BY record_id",
            (branch_id,),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def workflows(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT workflow_id, MIN(record_id) AS first FROM records"
            " GROUP BY workflow_id ORDER BY first"
        ).fetchall()
        return [r[0] for r in rows]

    def latest_workflow(self) -> str | None:
        row = self.conn.execute(
            "SELECT workflow_id FROM records ORDER BY record_id DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None

    # --------------------------------------------------------------- branches

    def create_branch(
        self,
        workflow_id: str,
        parent_branch_id: str | None = None,
        fork_point_record_id: int | None = None,
        depth: int = 0,
    ) -> Branch:
        bid = "br-" + uuid.uuid4().hex[:10]
        self.conn.execute(
            "INSERT INTO branches VALUES (?,?,?,?,?,?,?)",
            (
                bid,
                workflow_id,
                parent_branch_id,
                fork_point_record_id,
                depth,
                "active",
                time.time(),
            ),
        )
        return Branch(bid, parent_branch_id, fork_point_record_id, depth, "active")

    def branches(self, workflow_id: str) -> list[Branch]:
        rows = self.conn.execute(
            "SELECT branch_id, parent_branch_id, fork_point_record_id, depth, status"
            " FROM branches WHERE workflow_id=? ORDER BY created_ts, rowid",
            (workflow_id,),
        ).fetchall()
        return [Branch(*r) for r in rows]

    def branch(self, branch_id: str) -> Branch | None:
        row = self.conn.execute(
            "SELECT branch_id, parent_branch_id, fork_point_record_id, depth, status"
            " FROM branches WHERE branch_id=?",
            (branch_id,),
        ).fetchone()
        return Branch(*row) if row else None

    def active_branch(self, workflow_id: str) -> Branch | None:
        """The newest active branch."""
        for b in reversed(self.branches(workflow_id)):
            if b.status == "active":
                return b
        return None

    def set_branch_status(self, branch_id: str, status: BranchStatus) -> None:
        self.conn.execute("UPDATE branches SET status=? WHERE branch_id=?", (status, branch_id))

    def dead_branches(self, workflow_id: str) -> set[str]:
        return {
            b.branch_id for b in self.branches(workflow_id) if b.status in DEAD_BRANCH_STATUSES
        }

    # -------------------------------------------------------------- replay aids

    def next_seq(self, branch_id: str) -> int:
        row = self.conn.execute(
            "SELECT MAX(seq) FROM records WHERE branch_id=? AND kind IN ('INTENT','RESULT')",
            (branch_id,),
        ).fetchone()
        return 0 if row[0] is None else row[0] + 1

    def completed_results(self, branch_id: str) -> dict[int, JournalRecord]:
        """Steps this branch may skip on replay: those with a committed ``ok`` result."""
        out: dict[int, JournalRecord] = {}
        for r in self.branch_records(branch_id):
            if r.kind != "RESULT":
                continue
            if r.result and r.result.status == "ok":
                out[r.seq] = r
            else:
                out.pop(r.seq, None)
        return out

    def last_result(self, branch_id: str, seq: int) -> JournalRecord | None:
        rows = self.conn.execute(
            f"SELECT {_COLUMNS} FROM records WHERE branch_id=? AND seq=? AND kind='RESULT'"
            " ORDER BY record_id DESC LIMIT 1",
            (branch_id, seq),
        ).fetchall()
        return self._row_to_record(rows[0]) if rows else None

    # ------------------------------------------------------- barrier predicates

    def uncompensated(self, workflow_id: str) -> list[JournalRecord]:
        """The barrier predicate of 2.2, at workflow scope."""
        recs = self.records(workflow_id)
        dead = self.dead_branches(workflow_id)
        compensated = {
            r.key
            for r in recs
            if r.kind == "COMP_RESULT" and r.result and r.result.status == "ok"
        }
        out: list[JournalRecord] = []
        emitted: set[str] = set()
        for r in recs:
            if r.kind != "RESULT" or r.branch_id not in dead:
                continue
            if not r.effect_type or r.effect_type.reversibility != "compensatable":
                continue
            if not r.result or r.result.status != "ok":
                continue
            if r.key in compensated or r.key in emitted:
                continue
            emitted.add(r.key or "")
            out.append(r)
        return out

    def residue(self, workflow_id: str) -> list[JournalRecord]:
        """Irreversible effects stranded on abandoned branches (2.4)."""
        dead = self.dead_branches(workflow_id)
        seen: set[str] = set()
        out = []
        for r in self.records(workflow_id):
            if r.kind != "RESULT" or r.branch_id not in dead:
                continue
            if not r.effect_type or r.effect_type.reversibility != "irreversible":
                continue
            if not r.result or r.result.status not in ("ok", "unknown"):
                continue
            if r.key in seen:
                continue
            seen.add(r.key or "")
            out.append(r)
        return out

    def compensations(self, workflow_id: str) -> list[JournalRecord]:
        return [
            r
            for r in self.records(workflow_id)
            if r.kind == "COMP_RESULT" and r.result and r.result.status == "ok"
        ]

    # ----------------------------------------------------------------- leases

    def acquire_lease(self, scope: str, owner: str, ttl: float = 5.0) -> int:
        """Leadership is a row with an owner and an expiry."""
        now = time.time()

        # Compare-and-swap inside a write transaction.
        with self._tx_lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute(
                    "INSERT OR IGNORE INTO leases (scope, owner, epoch, expires)"
                    " VALUES (?,?,?,?)",
                    (scope, owner, 0, 0.0),
                )
                cur = self.conn.execute(
                    "UPDATE leases SET owner=?, epoch=epoch+1, expires=?"
                    " WHERE scope=? AND (owner=? OR expires<=?)",
                    (owner, now + ttl, scope, owner, now),
                )
                if cur.rowcount == 0:
                    held = self.conn.execute(
                        "SELECT owner, expires FROM leases WHERE scope=?", (scope,)
                    ).fetchone()
                    self.conn.execute("ROLLBACK")
                    raise LeaseUnavailable(
                        f"lease on {scope} held by {held[0]}"
                        f" for another {held[1] - now:.1f}s"
                    )
                epoch = self.conn.execute(
                    "SELECT epoch FROM leases WHERE scope=?", (scope,)
                ).fetchone()[0]
                self.conn.execute("COMMIT")
                return epoch
            except LeaseUnavailable:
                raise
            except Exception:
                try:
                    self.conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    def lease_info(self, scope: str) -> LeaseInfo | None:
        row = self.conn.execute(
            "SELECT owner, epoch, expires FROM leases WHERE scope=?", (scope,)
        ).fetchone()
        return LeaseInfo(scope, row[0], row[1], row[2]) if row else None

    def expire_lease(self, scope: str) -> None:
        """Force the current lease to be expired."""
        self.conn.execute("UPDATE leases SET expires=? WHERE scope=?", (0.0, scope))
