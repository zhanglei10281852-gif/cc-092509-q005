from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.deadlines.calendars import (
    adjust_forward,
    jurisdiction_timezone,
    remind_date_for,
    supported_jurisdictions,
)

# 规则注册表版本：规则或参数调整时递增，重算时只刷新未办结节点，已办结节点保留历史版本。
RULE_VERSION = 1


@dataclass(frozen=True, slots=True)
class JurisdictionRule:
    code: str
    jurisdiction: str
    kind: str
    kind_label: str
    anchor: str  # priority_date / earliest_priority / application_date / grant_date
    offset_months: int
    recurrence_months: int | None
    max_occurrences: int
    lead_days: int
    legal_basis: str


RULES: tuple[JurisdictionRule, ...] = (
    JurisdictionRule(
        code="CN.priority_expiry",
        jurisdiction="CN",
        kind="priority_expiry",
        kind_label="优先权期限",
        anchor="priority_date",
        offset_months=12,
        recurrence_months=None,
        max_occurrences=1,
        lead_days=30,
        legal_basis="《中华人民共和国专利法》第二十九条：外国优先权自在先申请日起十二个月内有效",
    ),
    JurisdictionRule(
        code="CN.publication",
        jurisdiction="CN",
        kind="publication",
        kind_label="发明专利公布",
        anchor="earliest_priority",
        offset_months=18,
        recurrence_months=None,
        max_occurrences=1,
        lead_days=14,
        legal_basis="《中华人民共和国专利法》第三十四条：自申请日（有优先权的自优先权日）起满十八个月即行公布",
    ),
    JurisdictionRule(
        code="CN.annuity",
        jurisdiction="CN",
        kind="annuity",
        kind_label="专利年费",
        anchor="application_date",
        offset_months=12,
        recurrence_months=12,
        max_occurrences=19,
        lead_days=30,
        legal_basis="《专利法实施细则》：年费应当在上一年度期满前缴纳，缴费届满日为申请日在该年的对应日",
    ),
    JurisdictionRule(
        code="US.priority_expiry",
        jurisdiction="US",
        kind="priority_expiry",
        kind_label="优先权期限",
        anchor="priority_date",
        offset_months=12,
        recurrence_months=None,
        max_occurrences=1,
        lead_days=30,
        legal_basis="35 U.S.C. §119 / 巴黎公约：优先权期限为十二个月",
    ),
    JurisdictionRule(
        code="US.publication",
        jurisdiction="US",
        kind="publication",
        kind_label="申请公开",
        anchor="earliest_priority",
        offset_months=18,
        recurrence_months=None,
        max_occurrences=1,
        lead_days=14,
        legal_basis="35 U.S.C. §122(b)：自最早申请日起满十八个月公开",
    ),
    JurisdictionRule(
        code="US.maintenance",
        jurisdiction="US",
        kind="maintenance",
        kind_label="维持费",
        anchor="grant_date",
        offset_months=42,
        recurrence_months=48,
        max_occurrences=3,
        lead_days=60,
        legal_basis="35 U.S.C. §41(b)、37 CFR 1.362：授权后 3.5 年、7.5 年、11.5 年缴纳维持费",
    ),
    JurisdictionRule(
        code="EP.priority_expiry",
        jurisdiction="EP",
        kind="priority_expiry",
        kind_label="优先权期限",
        anchor="priority_date",
        offset_months=12,
        recurrence_months=None,
        max_occurrences=1,
        lead_days=30,
        legal_basis="《欧洲专利公约》第87条：优先权期限为十二个月",
    ),
    JurisdictionRule(
        code="EP.publication",
        jurisdiction="EP",
        kind="publication",
        kind_label="申请公开",
        anchor="earliest_priority",
        offset_months=18,
        recurrence_months=None,
        max_occurrences=1,
        lead_days=14,
        legal_basis="《欧洲专利公约》第93条：自申请日或优先权日起满十八个月公开",
    ),
    JurisdictionRule(
        code="EP.renewal",
        jurisdiction="EP",
        kind="renewal",
        kind_label="续展费",
        anchor="application_date",
        offset_months=24,
        recurrence_months=12,
        max_occurrences=18,
        lead_days=30,
        legal_basis="《欧洲专利公约》第86条及细则第51条：第三年起按申请日对应日缴纳续展费",
    ),
    JurisdictionRule(
        code="JP.priority_expiry",
        jurisdiction="JP",
        kind="priority_expiry",
        kind_label="优先权期限",
        anchor="priority_date",
        offset_months=12,
        recurrence_months=None,
        max_occurrences=1,
        lead_days=30,
        legal_basis="特許法第43条：優先権の期間は十二か月",
    ),
    JurisdictionRule(
        code="JP.publication",
        jurisdiction="JP",
        kind="publication",
        kind_label="公開",
        anchor="earliest_priority",
        offset_months=18,
        recurrence_months=None,
        max_occurrences=1,
        lead_days=14,
        legal_basis="特許法第64条：出願日（優先権日）から一年六か月経過後に公開",
    ),
    JurisdictionRule(
        code="JP.annuity",
        jurisdiction="JP",
        kind="annuity",
        kind_label="特許料",
        anchor="grant_date",
        offset_months=12,
        recurrence_months=12,
        max_occurrences=17,
        lead_days=30,
        legal_basis="特許法第107条：第四年以降の特許料は各年度分を前年までに納付",
    ),
)


@dataclass(frozen=True, slots=True)
class PlannedNode:
    node_key: str
    kind: str
    kind_label: str
    occurrence: int
    occurrence_label: str
    timezone: str
    original_due_date: date
    base_due_date: date
    lead_days: int
    remind_at: datetime
    rule_code: str
    rule_version: int
    explanation: dict


def add_months(value: date, months: int) -> date:
    index = value.month - 1 + months
    year = value.year + index // 12
    month = index % 12 + 1
    day = min(value.day, monthrange(year, month)[1])
    return date(year, month, day)


def rules_for(jurisdiction: str) -> list[JurisdictionRule]:
    return [rule for rule in RULES if rule.jurisdiction == jurisdiction]


def rule_by_code(code: str) -> JurisdictionRule | None:
    for rule in RULES:
        if rule.code == code:
            return rule
    return None


def occurrence_label(rule: JurisdictionRule, occurrence: int) -> str:
    if rule.code == "CN.annuity":
        return f"第{occurrence + 1}年度年费"
    if rule.code == "EP.renewal":
        return f"第{occurrence + 2}年度续展费"
    if rule.code == "US.maintenance":
        return f"{('3.5', '7.5', '11.5')[occurrence - 1]}年维持费"
    if rule.code == "JP.annuity":
        return f"第{occurrence + 3}年度特许料"
    if rule.kind == "publication":
        return "满18个月公开"
    if rule.kind == "priority_expiry":
        return "12个月优先权期限"
    return f"第{occurrence}期"


def _anchor_date(rule: JurisdictionRule, case_dates: dict[str, date | None]) -> date | None:
    if rule.anchor == "priority_date":
        return case_dates.get("priority_date")
    if rule.anchor == "earliest_priority":
        candidates = [day for day in (case_dates.get("priority_date"), case_dates.get("application_date")) if day]
        return min(candidates) if candidates else None
    if rule.anchor == "application_date":
        return case_dates.get("application_date")
    if rule.anchor == "grant_date":
        return case_dates.get("grant_date")
    raise ValueError(f"未知锚点：{rule.anchor}")


def effective_schedule(jurisdiction: str, base_due: date, extension_days: int, lead_days: int) -> tuple[date, datetime]:
    """由基准截止日、人工延期天数和提醒提前量计算生效截止日与提醒时刻（UTC）。"""
    shifted = base_due + timedelta(days=extension_days)
    due, _ = adjust_forward(jurisdiction, shifted)
    remind_date, _ = remind_date_for(jurisdiction, due, lead_days)
    tz = ZoneInfo(jurisdiction_timezone(jurisdiction))
    remind_at = datetime.combine(remind_date, time.min, tzinfo=tz).astimezone(UTC)
    return due, remind_at


def plan_case_nodes(case_id: int, jurisdiction: str, case_dates: dict[str, date | None]) -> list[PlannedNode]:
    tz_name = jurisdiction_timezone(jurisdiction)
    planned: list[PlannedNode] = []
    for rule in rules_for(jurisdiction):
        anchor = _anchor_date(rule, case_dates)
        if anchor is None:
            continue
        for occurrence in range(1, rule.max_occurrences + 1):
            offset = rule.offset_months + (occurrence - 1) * (rule.recurrence_months or 0)
            original = add_months(anchor, offset)
            base, skipped = adjust_forward(jurisdiction, original)
            remind_date, remind_shifted = remind_date_for(jurisdiction, base, rule.lead_days)
            tz = ZoneInfo(tz_name)
            remind_at = datetime.combine(remind_date, time.min, tzinfo=tz).astimezone(UTC)
            explanation = {
                "rule_code": rule.code,
                "rule_version": RULE_VERSION,
                "legal_basis": rule.legal_basis,
                "anchor": {"field": rule.anchor, "date": anchor.isoformat()},
                "offset_months": offset,
                "occurrence": occurrence,
                "original_due_date": original.isoformat(),
                "holiday_adjustment": {
                    "applied": bool(skipped),
                    "skipped_dates": skipped,
                    "reason": "截止日落在周末或法定节假日，顺延至下一工作日" if skipped else "截止日本身为工作日，无需顺延",
                },
                "base_due_date": base.isoformat(),
                "timezone": tz_name,
                "reminder": {
                    "lead_days": rule.lead_days,
                    "remind_date": remind_date.isoformat(),
                    "shifted_earlier_for_non_working_day": remind_shifted,
                    "remind_at_utc": remind_at.isoformat(timespec="seconds"),
                    "policy": "提醒时间提前至前一工作日，节假日或法务人员休假期间临期事项不会静默消失",
                },
                "overdue_rule": f"以 {tz_name} 当地日期判定：当地日期晚于截止日才记为逾期，截止日当天不逾期",
            }
            planned.append(
                PlannedNode(
                    node_key=f"{case_id}:{rule.code}:{occurrence}",
                    kind=rule.kind,
                    kind_label=rule.kind_label,
                    occurrence=occurrence,
                    occurrence_label=occurrence_label(rule, occurrence),
                    timezone=tz_name,
                    original_due_date=original,
                    base_due_date=base,
                    lead_days=rule.lead_days,
                    remind_at=remind_at,
                    rule_code=rule.code,
                    rule_version=RULE_VERSION,
                    explanation=explanation,
                )
            )
    return planned


__all__ = [
    "RULE_VERSION",
    "RULES",
    "JurisdictionRule",
    "PlannedNode",
    "add_months",
    "effective_schedule",
    "occurrence_label",
    "plan_case_nodes",
    "rule_by_code",
    "rules_for",
    "supported_jurisdictions",
]
