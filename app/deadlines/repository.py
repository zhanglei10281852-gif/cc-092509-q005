from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.errors import NotFoundError
from app.deadlines.rules import PlannedNode


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class PatentCaseRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def create(self, data: dict[str, Any], owner_user_id: int, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO patent_cases(case_code,title,jurisdiction,application_number,application_date,
                                        priority_date,publication_date,grant_date,owner_user_id,state,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'active',?,?)""",
            (
                data["case_code"], data["title"], data["jurisdiction"], data["application_number"],
                data["application_date"], data.get("priority_date"), data.get("publication_date"),
                data.get("grant_date"), owner_user_id, now, now,
            ),
        )
        return self.get(cursor.lastrowid)

    def get(self, case_id: int) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM patent_cases WHERE id=?", (case_id,)).fetchone()
        if row is None:
            raise NotFoundError("专利案件不存在")
        return dict(row)

    def by_code(self, case_code: str) -> dict[str, Any] | None:
        return _row(self.connection.execute("SELECT * FROM patent_cases WHERE case_code=?", (case_code,)).fetchone())

    def list(self, *, jurisdiction: str | None = None, state: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if jurisdiction:
            clauses.append("jurisdiction=?")
            params.append(jurisdiction)
        if state:
            clauses.append("state=?")
            params.append(state)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(f"SELECT * FROM patent_cases{where} ORDER BY id", params).fetchall()
        return [dict(row) for row in rows]


class DeadlineNodeRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def insert(self, planned: PlannedNode, case_id: int, jurisdiction: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO deadline_nodes(node_key,case_id,jurisdiction,kind,kind_label,occurrence,occurrence_label,
                                          timezone,original_due_date,base_due_date,extension_days,due_date,lead_days,
                                          remind_at,status,rule_code,rule_version,explanation_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,0,?,?,?,'pending',?,?,?,?,?)""",
            (
                planned.node_key, case_id, jurisdiction, planned.kind, planned.kind_label,
                planned.occurrence, planned.occurrence_label, planned.timezone,
                planned.original_due_date.isoformat(), planned.base_due_date.isoformat(),
                planned.base_due_date.isoformat(), planned.lead_days,
                planned.remind_at.isoformat(timespec="seconds"), planned.rule_code, planned.rule_version,
                json.dumps(planned.explanation, ensure_ascii=False, sort_keys=True), now, now,
            ),
        )
        return self.get(cursor.lastrowid)

    def get(self, node_id: int) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM deadline_nodes WHERE id=?", (node_id,)).fetchone()
        if row is None:
            raise NotFoundError("期限节点不存在")
        return dict(row)

    def by_key(self, node_key: str) -> dict[str, Any] | None:
        return _row(self.connection.execute("SELECT * FROM deadline_nodes WHERE node_key=?", (node_key,)).fetchone())

    def list_nodes(
        self,
        *,
        status: str | None = None,
        case_id: int | None = None,
        jurisdiction: str | None = None,
        due_before: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if case_id:
            clauses.append("case_id=?")
            params.append(case_id)
        if jurisdiction:
            clauses.append("jurisdiction=?")
            params.append(jurisdiction)
        if due_before:
            clauses.append("due_date<=?")
            params.append(due_before)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            f"SELECT * FROM deadline_nodes{where} ORDER BY due_date,id", params
        ).fetchall()
        return [dict(row) for row in rows]

    def update_schedule(
        self,
        node_id: int,
        *,
        base_due_date: str,
        due_date: str,
        remind_at: str,
        lead_days: int,
        rule_version: int,
        explanation_json: str,
        now: str,
    ) -> None:
        # 只刷新未办结节点；done/cancelled 节点保留历史快照，不被重算改写。
        self.connection.execute(
            """UPDATE deadline_nodes
               SET base_due_date=?,due_date=?,remind_at=?,lead_days=?,rule_version=?,explanation_json=?,
                   version=version+1,updated_at=?
               WHERE id=? AND status IN ('pending','claimed')""",
            (base_due_date, due_date, remind_at, lead_days, rule_version, explanation_json, now, node_id),
        )

    def release_expired_claims(self, now: str) -> int:
        cursor = self.connection.execute(
            """UPDATE deadline_nodes
               SET status='pending',claimed_by=NULL,claimed_at=NULL,claim_expires_at=NULL,updated_at=?
               WHERE status='claimed' AND claim_expires_at<=?""",
            (now, now),
        )
        return cursor.rowcount

    def next_claimable(self, now: str) -> dict[str, Any] | None:
        return _row(
            self.connection.execute(
                """SELECT * FROM deadline_nodes
                   WHERE status='pending' AND remind_at<=?
                   ORDER BY due_date,id LIMIT 1""",
                (now,),
            ).fetchone()
        )

    def mark_claimed(self, node_id: int, worker: str, claimed_at: str, claim_expires_at: str, now: str) -> dict[str, Any] | None:
        cursor = self.connection.execute(
            """UPDATE deadline_nodes
               SET status='claimed',claimed_by=?,claimed_at=?,claim_expires_at=?,version=version+1,updated_at=?
               WHERE id=? AND status='pending'""",
            (worker, claimed_at, claim_expires_at, now, node_id),
        )
        if cursor.rowcount != 1:
            return None
        return self.get(node_id)

    def mark_completed(self, node_id: int, worker: str, resolution: str, completed_by: int, now: str) -> dict[str, Any] | None:
        cursor = self.connection.execute(
            """UPDATE deadline_nodes
               SET status='done',resolution=?,completed_at=?,completed_by=?,
                   claimed_by=NULL,claimed_at=NULL,claim_expires_at=NULL,version=version+1,updated_at=?
               WHERE id=? AND status='claimed' AND claimed_by=?""",
            (resolution, now, completed_by, now, node_id, worker),
        )
        if cursor.rowcount != 1:
            return None
        return self.get(node_id)

    def mark_released(self, node_id: int, worker: str, now: str) -> dict[str, Any] | None:
        cursor = self.connection.execute(
            """UPDATE deadline_nodes
               SET status='pending',claimed_by=NULL,claimed_at=NULL,claim_expires_at=NULL,version=version+1,updated_at=?
               WHERE id=? AND status='claimed' AND claimed_by=?""",
            (now, node_id, worker),
        )
        if cursor.rowcount != 1:
            return None
        return self.get(node_id)

    def apply_extension(
        self,
        node_id: int,
        *,
        extension_days: int,
        due_date: str,
        remind_at: str,
        expected_version: int,
        now: str,
    ) -> dict[str, Any] | None:
        cursor = self.connection.execute(
            """UPDATE deadline_nodes
               SET extension_days=?,due_date=?,remind_at=?,version=version+1,updated_at=?
               WHERE id=? AND status IN ('pending','claimed') AND version=?""",
            (extension_days, due_date, remind_at, now, node_id, expected_version),
        )
        if cursor.rowcount != 1:
            return None
        return self.get(node_id)

    def mark_paid(self, node_id: int, *, voucher_no: str, resolution: str, completed_by: int, now: str) -> dict[str, Any] | None:
        cursor = self.connection.execute(
            """UPDATE deadline_nodes
               SET status='done',payment_voucher=?,resolution=?,completed_at=?,completed_by=?,
                   claimed_by=NULL,claimed_at=NULL,claim_expires_at=NULL,version=version+1,updated_at=?
               WHERE id=? AND status IN ('pending','claimed')""",
            (voucher_no, resolution, now, completed_by, now, node_id),
        )
        if cursor.rowcount != 1:
            return None
        return self.get(node_id)


class DeadlineAdjustmentRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def append(
        self,
        node_id: int,
        adjustment_type: str,
        actor_user_id: int,
        *,
        days: int = 0,
        reason: str = "",
        voucher_no: str | None = None,
        before: dict[str, Any],
        after: dict[str, Any],
        now: str,
    ) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO deadline_adjustments(node_id,adjustment_type,actor_user_id,days,reason,voucher_no,
                                                before_json,after_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                node_id, adjustment_type, actor_user_id, days, reason, voucher_no,
                json.dumps(before, ensure_ascii=False, sort_keys=True),
                json.dumps(after, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )
        row = self.connection.execute("SELECT * FROM deadline_adjustments WHERE id=?", (cursor.lastrowid,)).fetchone()
        return dict(row)

    def list_for_node(self, node_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM deadline_adjustments WHERE node_id=? ORDER BY id", (node_id,)
        ).fetchall()
        return [dict(row) for row in rows]
