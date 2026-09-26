from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, ValidationError
from app.core.security import Principal
from app.deadlines.calendars import local_date, supported_jurisdictions
from app.deadlines.repository import (
    DeadlineAdjustmentRepository,
    DeadlineNodeRepository,
    PatentCaseRepository,
)
from app.deadlines.rules import effective_schedule, plan_case_nodes
from app.services.audit import AuditService

DATE_FIELDS = ("application_date", "priority_date", "publication_date", "grant_date")

DISPLAY_STATUS = {"pending": "待确认", "claimed": "待确认", "done": "已处理", "cancelled": "已取消"}


def _parse_date(value: Any, field: str) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValidationError(f"{field} 必须是 ISO 日期（YYYY-MM-DD）") from exc


class DeadlineService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.cases = PatentCaseRepository(connection)
        self.nodes = DeadlineNodeRepository(connection)
        self.adjustments = DeadlineAdjustmentRepository(connection)
        self.audit = AuditService(connection, self.clock)

    # ---------- 案件 ----------

    def register_case(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("deadlines.write")
        jurisdiction = str(data.get("jurisdiction", "")).strip().upper()
        if jurisdiction not in supported_jurisdictions():
            raise ValidationError(
                "暂不支持的司法辖区",
                context={"jurisdiction": jurisdiction, "supported": supported_jurisdictions()},
            )
        case_dates = {field: _parse_date(data.get(field), field) for field in DATE_FIELDS}
        if case_dates["application_date"] is None:
            raise ValidationError("申请日不能为空")
        if case_dates["priority_date"] and case_dates["priority_date"] > case_dates["application_date"]:
            raise ValidationError("优先权日不能晚于申请日")
        if case_dates["grant_date"] and case_dates["grant_date"] < case_dates["application_date"]:
            raise ValidationError("授权日不能早于申请日")
        case_code = str(data["case_code"]).strip()
        if self.cases.by_code(case_code):
            raise ConflictError("案件编号已经存在")
        now = to_storage(self.clock.now())
        payload = {**data, "case_code": case_code, "jurisdiction": jurisdiction}
        payload.update({field: day.isoformat() if day else None for field, day in case_dates.items()})
        owner = data.get("owner_user_id") or principal.user_id
        case = self.cases.create(payload, owner, now)
        generation = self.synchronize_case(case)
        self.audit.record(principal, "deadline_case.register", "patent_case", str(case["id"]), after=case)
        return {**case, "generation": generation}

    def list_cases(self, principal: Principal, jurisdiction: str | None = None) -> list[dict[str, Any]]:
        principal.require("deadlines.read")
        return self.cases.list(jurisdiction=jurisdiction.upper() if jurisdiction else None)

    def case_detail(self, principal: Principal, case_id: int, as_of: datetime | None = None) -> dict[str, Any]:
        principal.require("deadlines.read")
        case = self.cases.get(case_id)
        moment = as_of or self.clock.now()
        nodes = [self.present(node, moment) for node in self.nodes.list_nodes(case_id=case_id)]
        return {**case, "as_of": to_storage(moment), "nodes": nodes}

    # ---------- 节点生成与重算 ----------

    def synchronize_case(self, case: dict[str, Any]) -> dict[str, int]:
        """按规则幂等生成节点：重复运行不重复创建，已办结节点保持原样。"""
        case_dates = {field: _parse_date(case.get(field), field) for field in DATE_FIELDS}
        planned = plan_case_nodes(case["id"], case["jurisdiction"], case_dates)
        now = to_storage(self.clock.now())
        summary = {"created": 0, "updated": 0, "unchanged": 0, "preserved_closed": 0}
        for node in planned:
            existing = self.nodes.by_key(node.node_key)
            if existing is None:
                self.nodes.insert(node, case["id"], case["jurisdiction"], now)
                summary["created"] += 1
                continue
            if existing["status"] in ("done", "cancelled"):
                summary["preserved_closed"] += 1
                continue
            due, remind_at = effective_schedule(
                case["jurisdiction"], node.base_due_date, existing["extension_days"], node.lead_days
            )
            remind_text = remind_at.isoformat(timespec="seconds")
            explanation_json = json.dumps(node.explanation, ensure_ascii=False, sort_keys=True)
            changed = (
                existing["base_due_date"] != node.base_due_date.isoformat()
                or existing["due_date"] != due.isoformat()
                or existing["remind_at"] != remind_text
                or existing["rule_version"] != node.rule_version
                or existing["explanation_json"] != explanation_json
            )
            if changed:
                self.nodes.update_schedule(
                    existing["id"],
                    base_due_date=node.base_due_date.isoformat(),
                    due_date=due.isoformat(),
                    remind_at=remind_text,
                    lead_days=node.lead_days,
                    rule_version=node.rule_version,
                    explanation_json=explanation_json,
                    now=now,
                )
                summary["updated"] += 1
            else:
                summary["unchanged"] += 1
        return summary

    def regenerate_case(self, principal: Principal, case_id: int) -> dict[str, Any]:
        principal.require("deadlines.write")
        case = self.cases.get(case_id)
        summary = self.synchronize_case(case)
        self.audit.record(principal, "deadline_case.regenerate", "patent_case", str(case_id), after=summary)
        return {"case_id": case_id, **summary}

    def recompute(self, principal: Principal, as_of: datetime | None = None) -> dict[str, Any]:
        """全量重算：对所有在办案件重新套用规则，可在任意时间点执行，结果幂等。"""
        principal.require("deadlines.write")
        moment = as_of or self.clock.now()
        totals = {"created": 0, "updated": 0, "unchanged": 0, "preserved_closed": 0}
        details = []
        for case in self.cases.list(state="active"):
            summary = self.synchronize_case(case)
            for key in totals:
                totals[key] += summary[key]
            details.append({"case_id": case["id"], "case_code": case["case_code"], **summary})
        self.audit.record(principal, "deadline.recompute", "patent_case", None, after=totals)
        return {"as_of": to_storage(moment), "totals": totals, "cases": details}

    # ---------- 查询与状态呈现 ----------

    def display_status(self, node: dict[str, Any], as_of: datetime) -> str:
        if node["status"] in ("done", "cancelled"):
            return DISPLAY_STATUS[node["status"]]
        due = date.fromisoformat(node["due_date"])
        if local_date(node["jurisdiction"], as_of) > due:
            return "逾期"
        return "待确认"

    def present(self, node: dict[str, Any], as_of: datetime) -> dict[str, Any]:
        result = dict(node)
        result["explanation"] = json.loads(node["explanation_json"])
        result.pop("explanation_json", None)
        result["display_status"] = self.display_status(node, as_of)
        result["is_overdue"] = result["display_status"] == "逾期"
        return result

    def list_nodes(
        self,
        principal: Principal,
        *,
        as_of: datetime | None = None,
        status: str | None = None,
        display_status: str | None = None,
        case_id: int | None = None,
        jurisdiction: str | None = None,
        due_before: date | None = None,
    ) -> dict[str, Any]:
        principal.require("deadlines.read")
        moment = as_of or self.clock.now()
        rows = self.nodes.list_nodes(
            status=status,
            case_id=case_id,
            jurisdiction=jurisdiction.upper() if jurisdiction else None,
            due_before=due_before.isoformat() if due_before else None,
        )
        items = [self.present(row, moment) for row in rows]
        if display_status:
            items = [item for item in items if item["display_status"] == display_status]
        return {"as_of": to_storage(moment), "items": items}

    def node_detail(self, principal: Principal, node_id: int, as_of: datetime | None = None) -> dict[str, Any]:
        principal.require("deadlines.read")
        moment = as_of or self.clock.now()
        node = self.nodes.get(node_id)
        result = self.present(node, moment)
        result["adjustments"] = self.adjustments.list_for_node(node_id)
        return result

    def explain(self, principal: Principal, node_id: int, as_of: datetime | None = None) -> dict[str, Any]:
        """解释节点的规则来源：锚点、偏移、节假日调整、提醒策略与逾期判定口径。"""
        principal.require("deadlines.read")
        moment = as_of or self.clock.now()
        node = self.nodes.get(node_id)
        explanation = json.loads(node["explanation_json"])
        return {
            "node_id": node["id"],
            "node_key": node["node_key"],
            "case_id": node["case_id"],
            "jurisdiction": node["jurisdiction"],
            "display_status": self.display_status(node, moment),
            "as_of": to_storage(moment),
            "rule": {"code": node["rule_code"], "version": node["rule_version"]},
            "explanation": explanation,
            "extension_days": node["extension_days"],
            "adjustments": self.adjustments.list_for_node(node_id),
        }

    # ---------- 领取与办结（可恢复队列） ----------

    def claim(self, principal: Principal, worker: str, *, lease_seconds: int = 3600) -> dict[str, Any] | None:
        principal.require("deadlines.claim")
        now = self.clock.now()
        # 重启或执行者失联后，租约过期的节点自动回到可领取状态。
        self.nodes.release_expired_claims(to_storage(now))
        candidate = self.nodes.next_claimable(to_storage(now))
        if candidate is None:
            return None
        claimed = self.nodes.mark_claimed(
            candidate["id"],
            worker,
            to_storage(now),
            to_storage(now + timedelta(seconds=lease_seconds)),
            to_storage(now),
        )
        if claimed is None:
            return None
        self.audit.record(principal, "deadline_node.claim", "deadline_node", str(claimed["id"]), after={"worker": worker})
        return self.present(claimed, now)

    def complete(self, principal: Principal, node_id: int, worker: str, resolution: str) -> dict[str, Any]:
        principal.require("deadlines.claim")
        node = self.nodes.get(node_id)
        now = to_storage(self.clock.now())
        completed = self.nodes.mark_completed(node_id, worker, resolution, principal.user_id, now)
        if completed is None:
            if node["status"] == "done":
                raise ConflictError("节点已处理，旧任务不能重复完成或重新打开")
            raise ConflictError("节点未由当前执行者领取，或领取已过期")
        self.audit.record(principal, "deadline_node.complete", "deadline_node", str(node_id), after={"worker": worker, "resolution": resolution})
        return self.present(completed, self.clock.now())

    def release(self, principal: Principal, node_id: int, worker: str) -> dict[str, Any]:
        principal.require("deadlines.claim")
        self.nodes.get(node_id)
        now = to_storage(self.clock.now())
        released = self.nodes.mark_released(node_id, worker, now)
        if released is None:
            raise ConflictError("节点未由当前执行者领取")
        self.audit.record(principal, "deadline_node.release", "deadline_node", str(node_id), after={"worker": worker})
        return self.present(released, self.clock.now())

    # ---------- 人工登记：延期与缴费凭证 ----------

    def record_extension(self, principal: Principal, node_id: int, days: int, reason: str) -> dict[str, Any]:
        principal.require("deadlines.write")
        node = self.nodes.get(node_id)
        if node["status"] in ("done", "cancelled"):
            raise ConflictError("节点已办结，不能登记延期")
        new_days = node["extension_days"] + days
        due, remind_at = effective_schedule(
            node["jurisdiction"], date.fromisoformat(node["base_due_date"]), new_days, node["lead_days"]
        )
        now = to_storage(self.clock.now())
        updated = self.nodes.apply_extension(
            node_id,
            extension_days=new_days,
            due_date=due.isoformat(),
            remind_at=remind_at.isoformat(timespec="seconds"),
            expected_version=node["version"],
            now=now,
        )
        if updated is None:
            raise ConflictError("节点状态已变化，请刷新后重试")
        self.adjustments.append(
            node_id,
            "extension",
            principal.user_id,
            days=days,
            reason=reason,
            before={"due_date": node["due_date"], "extension_days": node["extension_days"]},
            after={"due_date": updated["due_date"], "extension_days": updated["extension_days"]},
            now=now,
        )
        self.audit.record(principal, "deadline_node.extend", "deadline_node", str(node_id), after={"days": days, "reason": reason})
        return self.present(updated, self.clock.now())

    def record_payment(self, principal: Principal, node_id: int, voucher_no: str, paid_at: date, note: str) -> dict[str, Any]:
        principal.require("deadlines.write")
        node = self.nodes.get(node_id)
        if node["status"] == "done":
            raise ConflictError("节点已处理，不能重复登记缴费凭证")
        if node["status"] == "cancelled":
            raise ConflictError("节点已取消，不能登记缴费凭证")
        resolution = f"已缴费，凭证号 {voucher_no}，缴费日 {paid_at.isoformat()}" + (f"；{note}" if note else "")
        now = to_storage(self.clock.now())
        updated = self.nodes.mark_paid(
            node_id, voucher_no=voucher_no, resolution=resolution, completed_by=principal.user_id, now=now
        )
        if updated is None:
            raise ConflictError("节点状态已变化，请刷新后重试")
        self.adjustments.append(
            node_id,
            "payment",
            principal.user_id,
            reason=note,
            voucher_no=voucher_no,
            before={"status": node["status"], "due_date": node["due_date"]},
            after={"status": "done", "payment_voucher": voucher_no, "paid_at": paid_at.isoformat()},
            now=now,
        )
        self.audit.record(principal, "deadline_node.payment", "deadline_node", str(node_id), after={"voucher_no": voucher_no})
        return self.present(updated, self.clock.now())
