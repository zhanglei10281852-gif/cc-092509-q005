"""专利期限服务：节点生成、提醒调度、人工动作与状态机。

设计要点
========

* **可恢复**：所有状态落在 SQLite。提醒通过 ``background_jobs`` 调度，
  崩溃后 ``running`` 租约过期会被重新领取；未发送的提醒在重启后
  :meth:`DeadlineService.recover_reminders` 中幂等补齐。
* **不重复**：提醒任务的去重键含节点、提前天数与当时的期满日；
  ``deadline_reminder_dispatches`` 上还有唯一约束兜底，重复运行
  不会产生第二条提醒。
* **已处理不被重开**：重算只补全新节点、更新未处理节点；``handled``
  节点一律跳过。旧提醒任务即使触发，worker 也会空跑跳过。
* **时区安全**：逾期判定只比较辖区本地历法日（见 :mod:`app.deadlines.rules`）。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import Principal
from app.deadlines.rules import (
    RULES_VERSION,
    GeneratedNode,
    RuleContext,
    get_calendar,
    local_day_to_utc,
    local_today,
    supported_jurisdictions,
)
from app.services.audit import AuditContext, AuditService
from app.services.jobs import JobService

REMINDER_JOB_TYPE = "deadline.reminder"
DEFAULT_LEAD_DAYS = (30, 7, 0)
NODE_STATUSES = ("pending", "handled", "overdue")


class DeadlineService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.jobs = JobService(connection, self.clock)
        self.audit = AuditService(connection, self.clock)

    # ------------------------------------------------------------------
    # 申请登记
    # ------------------------------------------------------------------

    def register_application(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("deadlines.write")
        now = to_storage(self.clock.now())
        existing = self.connection.execute(
            "SELECT * FROM patent_applications WHERE application_code=?",
            (data["application_code"],),
        ).fetchone()
        if existing:
            application = dict(existing)
            if application["application_number"] != data["application_number"] or application["jurisdiction"] != data["jurisdiction"]:
                raise ConflictError("申请编号已被不同辖区/申请号占用")
            return {"application": application, "nodes": self.list_nodes(application_id=application["id"]), "replayed": True}

        cursor = self.connection.execute(
            """INSERT INTO patent_applications(
                   application_code,dossier_id,jurisdiction,application_number,title,
                   filing_date,priority_date,publication_date,grant_date,
                   annuities_paid,rules_version,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                data["application_code"],
                data.get("dossier_id"),
                data["jurisdiction"],
                data["application_number"],
                data.get("title", ""),
                self._iso(data["filing_date"]),
                self._iso_optional(data.get("priority_date")),
                self._iso_optional(data.get("publication_date")),
                self._iso_optional(data.get("grant_date")),
                0,
                RULES_VERSION,
                now,
                now,
            ),
        )
        application = self._get_application(cursor.lastrowid)
        created = self._sync_nodes(application, lead_days=tuple(data.get("lead_days") or DEFAULT_LEAD_DAYS))
        self.audit.record(
            principal,
            "deadline.application.register",
            "patent_application",
            str(application["id"]),
            after={"jurisdiction": application["jurisdiction"], "nodes_created": created},
        )
        return {"application": application, "nodes": self.list_nodes(application_id=application["id"]), "replayed": False}

    def update_anchors(self, principal: Principal, application_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("deadlines.write")
        application = self._get_application(application_id)
        fields = ("priority_date", "publication_date", "grant_date", "title")
        assignments: list[str] = []
        values: list[Any] = []
        for field_name in fields:
            if field_name in data and data[field_name] is not None:
                assignments.append(f"{field_name}=?")
                values.append(self._iso(data[field_name]) if field_name.endswith("_date") else data[field_name])
        if "annuities_paid" in data and data["annuities_paid"] is not None:
            assignments.append("annuities_paid=?")
            values.append(int(data["annuities_paid"]))
        if not assignments:
            raise ValidationError("没有需要更新的锚点字段")
        before = dict(application)
        assignments.append("rules_version=?")
        values.append(RULES_VERSION)
        assignments.append("updated_at=?")
        values.append(to_storage(self.clock.now()))
        values.append(application_id)
        self.connection.execute(f"UPDATE patent_applications SET {','.join(assignments)} WHERE id=?", values)
        application = self._get_application(application_id)
        changed = self._sync_nodes(application)
        self.audit.record(
            principal,
            "deadline.application.anchors_update",
            "patent_application",
            str(application_id),
            before=before,
            after=application,
            metadata={"nodes_recomputed": changed},
        )
        return {"application": application, "nodes": self.list_nodes(application_id=application_id)}

    # ------------------------------------------------------------------
    # 重算
    # ------------------------------------------------------------------

    def recompute(self, principal: Principal, application_id: int) -> dict[str, Any]:
        """按当前锚点与最新版规则立即重算并落库（幂等）。"""
        principal.require("deadlines.write")
        application = self._get_application(application_id)
        changed = self._sync_nodes(application)
        self.audit.record(
            principal,
            "deadline.recompute",
            "patent_application",
            str(application_id),
            metadata={"nodes_changed": changed, "rules_version": RULES_VERSION},
        )
        return {
            "application": application,
            "nodes": self.list_nodes(application_id=application_id),
            "rules_version": RULES_VERSION,
            "nodes_changed": changed,
        }

    def preview_at(self, principal: Principal, application_id: int, as_of: date) -> dict[str, Any]:
        """按任意时间点重算节点（不落库），并解释每条规则来源。"""
        principal.require("deadlines.read")
        application = self._get_application(application_id)
        generated = self._generate(application)
        stored = {
            row["node_key"]: dict(row)
            for row in self.connection.execute(
                "SELECT * FROM deadline_nodes WHERE application_id=?", (application_id,)
            ).fetchall()
        }
        nodes: list[dict[str, Any]] = []
        for node in generated:
            persisted = stored.get(node.node_key)
            # 已处理的事实不随“假如时间点”改变
            if persisted and persisted["status"] == "handled":
                effective = "handled"
            else:
                effective = "overdue" if as_of > node.due_date else "pending"
            nodes.append(
                {
                    "node_key": node.node_key,
                    "kind": node.kind,
                    "title": node.title,
                    "sequence_no": node.sequence_no,
                    "due_date": node.due_date.isoformat(),
                    "nominal_due_date": node.nominal_due_date.isoformat(),
                    "rule_code": node.rule_code,
                    "rule_source": node.rule_source,
                    "rules_version": RULES_VERSION,
                    "explanation": node.explanation,
                    "status_as_of": effective,
                    "persisted_status": persisted["status"] if persisted else None,
                }
            )
        return {
            "application_id": application_id,
            "jurisdiction": application["jurisdiction"],
            "as_of": as_of.isoformat(),
            "jurisdiction_timezone": get_calendar(application["jurisdiction"]).timezone,
            "rules_version": RULES_VERSION,
            "nodes": nodes,
        }

    def _sync_nodes(self, application: dict[str, Any], *, lead_days: tuple[int, ...] = DEFAULT_LEAD_DAYS) -> dict[str, int]:
        """把规则引擎结果与库中节点对齐。返回 {created, updated} 数量。"""
        generated = self._generate(application)
        now = to_storage(self.clock.now())
        created = updated = 0
        for node in generated:
            existing = self.connection.execute(
                "SELECT * FROM deadline_nodes WHERE application_id=? AND node_key=?",
                (application["id"], node.node_key),
            ).fetchone()
            if existing is None:
                cursor = self.connection.execute(
                    """INSERT INTO deadline_nodes(
                           application_id,node_key,kind,title,due_date,original_due_date,
                           nominal_due_date,status,sequence_no,rule_code,rule_source,
                           rules_version,explanation,lead_days_json,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,'pending',?,?,?,?,?,?,?,?)""",
                    (
                        application["id"],
                        node.node_key,
                        node.kind,
                        node.title,
                        node.due_date.isoformat(),
                        node.due_date.isoformat(),
                        node.nominal_due_date.isoformat(),
                        node.sequence_no,
                        node.rule_code,
                        node.rule_source,
                        RULES_VERSION,
                        node.explanation,
                        json.dumps(list(lead_days)),
                        now,
                        now,
                    ),
                )
                row = dict(self.connection.execute("SELECT * FROM deadline_nodes WHERE id=?", (cursor.lastrowid,)).fetchone())
                self._schedule_reminders(row)
                created += 1
                continue

            if existing["status"] == "handled":
                # 已处理事项永不被重算或旧任务重新打开
                continue

            shifts = existing["due_date"] != node.due_date.isoformat()
            if shifts or existing["rules_version"] != RULES_VERSION:
                self.connection.execute(
                    """UPDATE deadline_nodes
                       SET due_date=?,nominal_due_date=?,title=?,rule_code=?,rule_source=?,
                           rules_version=?,explanation=?,status=CASE WHEN status='overdue' THEN 'pending' ELSE status END,
                           confirmed_at=NULL,updated_at=?
                       WHERE id=?""",
                    (
                        node.due_date.isoformat(),
                        node.nominal_due_date.isoformat(),
                        node.title,
                        node.rule_code,
                        node.rule_source,
                        RULES_VERSION,
                        node.explanation,
                        now,
                        existing["id"],
                    ),
                )
                row = dict(self.connection.execute("SELECT * FROM deadline_nodes WHERE id=?", (existing["id"],)).fetchone())
                self._schedule_reminders(row, replace_all=True)
                updated += 1
            else:
                row = dict(existing)
                self._schedule_reminders(row)
        self.sweep_overdue()
        return {"created": created, "updated": updated}

    def _generate(self, application: dict[str, Any]) -> list[GeneratedNode]:
        calendar = get_calendar(application["jurisdiction"])
        context = RuleContext(
            filing_date=date.fromisoformat(application["filing_date"]),
            priority_date=self._date_optional(application["priority_date"]),
            publication_date=self._date_optional(application["publication_date"]),
            grant_date=self._date_optional(application["grant_date"]),
            annuities_paid=application["annuities_paid"],
        )
        return calendar.generate_nodes(context)

    # ------------------------------------------------------------------
    # 提醒调度
    # ------------------------------------------------------------------

    def _schedule_reminders(self, node: dict[str, Any], *, replace_all: bool = False) -> None:
        """为节点幂等安排各提前量提醒；期满日变化时取消旧任务并启用新一代排程。"""
        application = self._get_application(node["application_id"])
        timezone_name = get_calendar(application["jurisdiction"]).timezone
        due = date.fromisoformat(node["due_date"])
        now = self.clock.now()
        generation = int(node.get("schedule_generation") or 0)
        # 恢复场景：若某条排程的任务已被取消/失败且从未发送，提升代次重建
        broken = self.connection.execute(
            "SELECT 1 FROM deadline_reminder_dispatches d JOIN background_jobs j ON j.id=d.job_id "
            "WHERE d.node_id=? AND d.dispatched_at IS NULL AND j.status IN ('cancelled','failed') LIMIT 1",
            (node["id"],),
        ).fetchone()
        if replace_all or broken is not None:
            generation += 1
            self.connection.execute(
                "UPDATE deadline_nodes SET schedule_generation=? WHERE id=?",
                (generation, node["id"]),
            )
        for lead_days in json.loads(node["lead_days_json"]):
            fire_day = due - timedelta(days=int(lead_days))
            fire_at = local_day_to_utc(fire_day, timezone_name, hour=9)
            delay = max(0, int((fire_at - now).total_seconds()))
            payload = {
                "node_id": node["id"],
                "application_id": node["application_id"],
                "lead_days": int(lead_days),
                "due_date": node["due_date"],
                "jurisdiction": application["jurisdiction"],
            }
            # 去重键含排程代次：同一提前量在延期/重开后可以重新触发，
            # 而同代次重复运行仍命中同一条任务，绝不重复创建。
            dedup_key = f"deadline-reminder:g{generation}:{node['id']}:{lead_days}"
            existing = self.connection.execute(
                "SELECT * FROM deadline_reminder_dispatches WHERE node_id=? AND lead_days=? AND schedule_generation=?",
                (node["id"], lead_days, generation),
            ).fetchone()
            if existing is not None:
                continue
            stale = self.connection.execute(
                "SELECT * FROM deadline_reminder_dispatches WHERE node_id=? AND lead_days=? AND schedule_generation<>?",
                (node["id"], lead_days, generation),
            ).fetchall()
            job = self.jobs.enqueue(REMINDER_JOB_TYPE, dedup_key, payload, delay_seconds=delay)
            for row in stale:
                self._cancel_pending_job(row["job_id"])
                self.connection.execute("DELETE FROM deadline_reminder_dispatches WHERE id=?", (row["id"],))
            self.connection.execute(
                """INSERT INTO deadline_reminder_dispatches(
                       node_id,lead_days,scheduled_due_date,job_id,dispatched_at,schedule_generation
                   ) VALUES(?,?,?,?,NULL,?)""",
                (node["id"], lead_days, node["due_date"], job["id"], generation),
            )

    def _cancel_pending_job(self, job_id: int) -> None:
        self.connection.execute(
            "UPDATE background_jobs SET status='cancelled',updated_at=? WHERE id=? AND status='pending'",
            (to_storage(self.clock.now()), job_id),
        )

    def recover_reminders(self) -> dict[str, int]:
        """重启恢复：为每个未处理节点补齐/校验提醒任务。完全幂等。"""
        rows = self.connection.execute(
            "SELECT * FROM deadline_nodes WHERE status!='handled'"
        ).fetchall()
        for row in rows:
            self._schedule_reminders(dict(row))
        self.sweep_overdue()
        return {"open_nodes": len(rows)}

    # ------------------------------------------------------------------
    # 逾期扫描（辖区本地历法日比较）
    # ------------------------------------------------------------------

    def sweep_overdue(self, *, moment: datetime | None = None) -> int:
        moment = moment or self.clock.now()
        count = 0
        rows = self.connection.execute(
            "SELECT n.*, a.jurisdiction FROM deadline_nodes n "
            "JOIN patent_applications a ON a.id=n.application_id "
            "WHERE n.status='pending'"
        ).fetchall()
        for row in rows:
            timezone_name = get_calendar(row["jurisdiction"]).timezone
            today = local_today(moment, timezone_name)
            due = date.fromisoformat(row["due_date"])
            if today > due:  # 同一天绝不判逾期
                self.connection.execute(
                    "UPDATE deadline_nodes SET status='overdue',updated_at=? WHERE id=? AND status='pending'",
                    (to_storage(moment), row["id"]),
                )
                count += 1
        return count

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def list_nodes(
        self,
        *,
        principal: Principal | None = None,
        application_id: int | None = None,
        jurisdiction: str | None = None,
        status: str | None = None,
        as_of: date | None = None,
    ) -> list[dict[str, Any]]:
        if principal is not None:
            principal.require("deadlines.read")
        sql = (
            "SELECT n.*, a.application_code,a.application_number,a.jurisdiction "
            "FROM deadline_nodes n JOIN patent_applications a ON a.id=n.application_id"
        )
        clauses: list[str] = []
        params: list[Any] = []
        if application_id is not None:
            clauses.append("n.application_id=?")
            params.append(application_id)
        if jurisdiction:
            clauses.append("a.jurisdiction=?")
            params.append(jurisdiction)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY n.due_date, n.id"
        rows = [dict(row) for row in self.connection.execute(sql, params).fetchall()]
        if as_of is None:
            # 读时兜底刷新逾期状态，避免扫描器未跑导致列表失真
            self.sweep_overdue()
            rows = [dict(row) for row in self.connection.execute(sql, params).fetchall()]
        result = []
        for row in rows:
            effective = row["status"]
            if as_of is not None:
                if effective != "handled":
                    effective = "overdue" if as_of > date.fromisoformat(row["due_date"]) else "pending"
            row["effective_status"] = effective
            if status and effective != status:
                continue
            result.append(row)
        return result

    def list_applications(self, principal: Principal) -> list[dict[str, Any]]:
        principal.require("deadlines.read")
        return [dict(row) for row in self.connection.execute("SELECT * FROM patent_applications ORDER BY id").fetchall()]

    def get_node(self, node_id: int) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM deadline_nodes WHERE id=?", (node_id,)).fetchone()
        if row is None:
            raise NotFoundError("期限节点不存在")
        return dict(row)

    def list_actions(self, principal: Principal, node_id: int) -> list[dict[str, Any]]:
        principal.require("deadlines.read")
        self.get_node(node_id)
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM deadline_actions WHERE node_id=? ORDER BY id", (node_id,)
            ).fetchall()
        ]

    def explain_rule(self, principal: Principal, rule_code: str) -> dict[str, Any]:
        principal.require("deadlines.read")
        for jurisdiction in supported_jurisdictions():
            for rule in get_calendar(jurisdiction).rules:
                if rule.code == rule_code:
                    return {"jurisdiction": jurisdiction, **rule.catalog(), "rules_version": RULES_VERSION}
        raise NotFoundError("规则不存在")

    # ------------------------------------------------------------------
    # 人工动作
    # ------------------------------------------------------------------

    def confirm(self, principal: Principal, node_id: int, note: str = "") -> dict[str, Any]:
        principal.require("deadlines.write")
        node = self.get_node(node_id)
        if node["status"] == "handled":
            raise ConflictError("已处理节点无需确认")
        now = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE deadline_nodes SET confirmed_at=?,confirmed_by=?,updated_at=? WHERE id=?",
            (now, principal.user_id, now, node_id),
        )
        self._record_action(principal, node, "confirm", note=note)
        return self.get_node(node_id)

    def extend(
        self,
        principal: Principal,
        node_id: int,
        new_due_date: date,
        *,
        evidence_reference: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        principal.require("deadlines.write")
        node = self.get_node(node_id)
        if node["status"] == "handled":
            raise ConflictError("已处理节点不能延期；如需重新跟踪请显式重开")
        new_iso = new_due_date.isoformat()
        if new_iso <= node["due_date"]:
            raise ValidationError("延期后的期满日必须晚于当前期满日")
        now = to_storage(self.clock.now())
        self.connection.execute(
            """UPDATE deadline_nodes
               SET due_date=?,status='pending',confirmed_at=NULL,extension_count=extension_count+1,updated_at=?
               WHERE id=?""",
            (new_iso, now, node_id),
        )
        refreshed = self.get_node(node_id)
        self._schedule_reminders(refreshed, replace_all=True)
        self._record_action(
            principal,
            node,
            "extend",
            previous=node["due_date"],
            new=new_iso,
            evidence_reference=evidence_reference,
            note=note,
        )
        self.sweep_overdue()
        return self.get_node(node_id)

    def record_payment(
        self,
        principal: Principal,
        node_id: int,
        *,
        evidence_reference: str,
        note: str = "",
        paid_at: date | None = None,
    ) -> dict[str, Any]:
        principal.require("deadlines.write")
        if not evidence_reference.strip():
            raise ValidationError("必须登记缴费凭证编号或收据参考")
        node = self.get_node(node_id)
        if node["status"] == "handled":
            return {**node, "replayed": True}
        paid_day = paid_at or self._local_today(node)
        now = to_storage(self.clock.now())
        self.connection.execute(
            """UPDATE deadline_nodes
               SET status='handled',handled_at=?,handled_by=?,updated_at=? WHERE id=?""",
            (to_storage(self.clock.now()), principal.user_id, now, node_id),
        )
        self._cancel_node_pending_jobs(node_id)
        self._record_action(
            principal,
            node,
            "payment",
            previous=node["due_date"],
            new=paid_day.isoformat(),
            evidence_reference=evidence_reference.strip(),
            note=note,
        )
        # 年费缴付后推进申请的缴费进度，从而生成下一年度节点
        if node["kind"] == "annuity" and node["sequence_no"] is not None:
            application = self._get_application(node["application_id"])
            if node["sequence_no"] > application["annuities_paid"]:
                self.connection.execute(
                    "UPDATE patent_applications SET annuities_paid=?,updated_at=? WHERE id=?",
                    (node["sequence_no"], now, application["id"]),
                )
                application = self._get_application(application["id"])
                self._sync_nodes(application)
        self.audit.record(
            principal,
            "deadline.payment.recorded",
            "deadline_node",
            str(node_id),
            after={"evidence_reference": evidence_reference.strip(), "node_key": node["node_key"]},
        )
        return self.get_node(node_id)

    def reopen(self, principal: Principal, node_id: int, *, note: str = "") -> dict[str, Any]:
        principal.require("deadlines.write")
        node = self.get_node(node_id)
        if node["status"] != "handled":
            raise ConflictError("只有已处理节点可以重开")
        now = to_storage(self.clock.now())
        self.connection.execute(
            """UPDATE deadline_nodes
               SET status='pending',handled_at=NULL,handled_by=NULL,updated_at=? WHERE id=?""",
            (now, node_id),
        )
        refreshed = self.get_node(node_id)
        self._schedule_reminders(refreshed, replace_all=True)
        self._record_action(principal, node, "reopen", note=note)
        self.sweep_overdue()
        return self.get_node(node_id)

    def _cancel_node_pending_jobs(self, node_id: int) -> None:
        rows = self.connection.execute(
            "SELECT job_id FROM deadline_reminder_dispatches WHERE node_id=?", (node_id,)
        ).fetchall()
        for row in rows:
            self._cancel_pending_job(row["job_id"])

    def _record_action(
        self,
        principal: Principal,
        node: dict[str, Any],
        action_type: str,
        *,
        previous: str | None = None,
        new: str | None = None,
        evidence_reference: str = "",
        note: str = "",
    ) -> None:
        self.connection.execute(
            """INSERT INTO deadline_actions(
                   node_id,application_id,action_type,previous_due_date,new_due_date,
                   evidence_reference,note,actor_user_id,actor_name,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                node["id"],
                node["application_id"],
                action_type,
                previous,
                new,
                evidence_reference,
                note,
                principal.user_id,
                principal.display_name,
                to_storage(self.clock.now()),
            ),
        )

    # ------------------------------------------------------------------
    # 提醒任务执行（worker）
    # ------------------------------------------------------------------

    def dispatch_due_reminders(self, worker: str, *, lease_seconds: int = 60) -> int:
        """领取并处理到期的提醒任务，返回处理条数。崩溃安全、幂等。"""
        processed = 0
        while True:
            job = self.jobs.claim(worker, lease_seconds=lease_seconds, job_type=REMINDER_JOB_TYPE)
            if job is None:
                break
            try:
                result = self._deliver_reminder(job)
            except Exception as exc:  # pragma: no cover - 防御性
                self.jobs.fail(job["id"], worker, str(exc), retry_seconds=300)
                continue
            self.jobs.complete(job["id"], worker, result)
            processed += 1
        return processed

    def _deliver_reminder(self, job: dict[str, Any]) -> dict[str, Any]:
        payload = json.loads(job["payload_json"])
        node_row = self.connection.execute(
            "SELECT n.*, a.jurisdiction FROM deadline_nodes n "
            "JOIN patent_applications a ON a.id=n.application_id WHERE n.id=?",
            (payload["node_id"],),
        ).fetchone()
        if node_row is None:
            return {"delivered": False, "reason": "node_missing"}
        node = dict(node_row)
        if node["status"] == "handled":
            # 已完成事项：旧任务空跑，绝不重新打开
            return {"delivered": False, "reason": "already_handled"}
        dispatch = self.connection.execute(
            "SELECT * FROM deadline_reminder_dispatches WHERE job_id=?", (job["id"],)
        ).fetchone()
        if dispatch is None:
            # 不存在排程登记：属于已被延期/重开取代的旧代次任务，空跑跳过
            return {"delivered": False, "reason": "stale_schedule"}
        delivered_twice = dispatch["dispatched_at"] is not None
        if not delivered_twice:
            self.connection.execute(
                "UPDATE deadline_reminder_dispatches SET dispatched_at=? WHERE id=?",
                (to_storage(self.clock.now()), dispatch["id"]),
            )
        self.audit.record(
            AuditContext(actor_user_id=None, actor_name=f"deadline-worker:{job['locked_by']}"),
            "deadline.reminder.dispatched",
            "deadline_node",
            str(node["id"]),
            metadata={"lead_days": payload.get("lead_days"), "duplicate": delivered_twice},
        )
        return {
            "delivered": not delivered_twice,
            "node_id": node["id"],
            "node_key": node["node_key"],
            "due_date": node["due_date"],
            "lead_days": payload.get("lead_days"),
        }

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _get_application(self, application_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM patent_applications WHERE id=?", (application_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("专利申请不存在")
        return dict(row)

    def _local_today(self, node: dict[str, Any]) -> date:
        jurisdiction = node.get("jurisdiction")
        if jurisdiction is None:
            application = self._get_application(node["application_id"])
            jurisdiction = application["jurisdiction"]
        timezone_name = get_calendar(jurisdiction).timezone
        return local_today(self.clock.now(), timezone_name)

    @staticmethod
    def _iso(value: Any) -> str:
        if isinstance(value, date):
            return value.isoformat()
        return str(value)

    @staticmethod
    def _iso_optional(value: Any) -> str | None:
        if value is None:
            return None
        return DeadlineService._iso(value)

    @staticmethod
    def _date_optional(value: str | None) -> date | None:
        return date.fromisoformat(value) if value else None
