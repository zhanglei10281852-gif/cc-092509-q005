"""司法辖区期限规则。

规则引擎是无状态的纯函数，所有期限都以辖区本地历法日（``date``）计算，
不混入任何时区的时分秒。时区换算只发生在两个边界：

1. 判断“今天是否已超过期满日”时，把 UTC 时刻换算到辖区时区再取历法日；
2. 安排提醒任务时，把辖区本地日期换算回 UTC 时刻。

这样同一历法日不会因为观测者跨时区而被误判为逾期。
"""
from __future__ import annotations

import calendar as _calendar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

# 规则集版本。规则条款或假日表发生变化时抬升版本，节点上会留存生成时版本，
# 便于解释“这个日期是按哪一版规则算出来的”。
RULES_VERSION = "2026-09"

# ---------------------------------------------------------------------------
# 辖区假日表（固定日期的演示数据，可替换为数据库或外部日历服务）。
# ---------------------------------------------------------------------------
_HOLIDAYS: dict[str, frozenset[date]] = {
    "CN": frozenset(
        {
            date(2026, 1, 1),
            date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18),
            date(2026, 2, 19), date(2026, 2, 20), date(2026, 2, 21),
            date(2026, 4, 6),
            date(2026, 5, 1), date(2026, 5, 4), date(2026, 5, 5),
            date(2026, 6, 19),
            date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5),
            date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8),
        }
    ),
    "US": frozenset(
        {
            date(2026, 1, 1),
            date(2026, 1, 19),
            date(2026, 5, 25),
            date(2026, 7, 3),
            date(2026, 9, 7),
            date(2026, 10, 12),
            date(2026, 11, 11),
            date(2026, 11, 26),
            date(2026, 12, 25),
        }
    ),
    "EP": frozenset(
        {
            date(2026, 1, 1),
            date(2026, 4, 3), date(2026, 4, 6),
            date(2026, 5, 1),
            date(2026, 12, 25), date(2026, 12, 28),
        }
    ),
}

# 辖区官方历法所在时区；“今天是几号”按此时区切分。
JURISDICTION_TIMEZONES: dict[str, str] = {
    "CN": "Asia/Shanghai",
    "US": "America/New_York",
    "EP": "Europe/Berlin",
}


# ---------------------------------------------------------------------------
# 历法工具
# ---------------------------------------------------------------------------

def add_months(day: date, months: int) -> date:
    """按月推进日期；目标月没有对应日（如 2 月 29 日）时取该月最后一天。"""
    index = day.year * 12 + (day.month - 1) + months
    year, zero_month = divmod(index, 12)
    month = zero_month + 1
    last_day = _calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))


def add_years(day: date, years: int) -> date:
    return add_months(day, years * 12)


def is_weekend(day: date) -> bool:
    return day.weekday() >= 5


def is_holiday(jurisdiction: str, day: date) -> bool:
    return day in _HOLIDAYS.get(jurisdiction, frozenset())


def is_non_business_day(jurisdiction: str, day: date) -> bool:
    return is_weekend(day) or is_holiday(jurisdiction, day)


def roll_forward(jurisdiction: str, day: date, *, maximum_rolls: int = 60) -> tuple[date, str | None]:
    """到期日落在非工作日时顺延到下一个工作日，返回 (期满日, 顺延说明)。"""
    original = day
    reasons: list[str] = []
    if is_holiday(jurisdiction, day):
        reasons.append("公共假日")
    if is_weekend(day):
        reasons.append("周末")
    steps = 0
    while is_non_business_day(jurisdiction, day):
        day += timedelta(days=1)
        steps += 1
        if steps > maximum_rolls:
            raise ValueError(f"辖区 {jurisdiction} 假日表可能缺失，顺延超过 {maximum_rolls} 天")
    if day == original:
        return day, None
    return day, f"到期日为{'且'.join(reasons)}，顺延至下一工作日 {day.isoformat()}"


def local_today(now_utc: datetime, timezone_name: str) -> date:
    """把 UTC 时刻换算到辖区时区后取历法日——逾期判定只允许走这一入口。"""
    if now_utc.tzinfo is None:
        raise ValueError("判定逾期必须使用带时区的时刻")
    return now_utc.astimezone(ZoneInfo(timezone_name)).date()


def local_day_to_utc(day: date, timezone_name: str, *, hour: int = 9, minute: int = 0) -> datetime:
    """辖区本地某日的约定时刻换算为 UTC，用于安排提醒。"""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo(timezone_name)).astimezone(
        ZoneInfo("UTC")
    )


# ---------------------------------------------------------------------------
# 规则模型
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class RuleContext:
    """生成节点时可使用的已知日期锚点与缴费进度。"""

    filing_date: date | None = None
    priority_date: date | None = None
    publication_date: date | None = None
    grant_date: date | None = None
    annuities_paid: int = 0


_ANCHOR_LABELS = {
    "filing_date": "申请日",
    "priority_date": "优先权日",
    "publication_date": "公开日",
    "grant_date": "授权日",
}


@dataclass(frozen=True, slots=True)
class GeneratedNode:
    """规则引擎产出的候选节点（尚未持久化）。"""

    node_key: str
    kind: str
    due_date: date
    nominal_due_date: date
    title: str
    rule_code: str
    rule_source: str
    explanation: str
    sequence_no: int | None = None


@dataclass(frozen=True, slots=True)
class JurisdictionCalendar:
    jurisdiction: str
    timezone: str
    rules: tuple["Rule", ...]
    holiday_dates: frozenset[date] = field(default_factory=frozenset)

    def explain_day(self, day: date) -> str:
        labels: list[str] = []
        if is_holiday(self.jurisdiction, day):
            labels.append("公共假日")
        if is_weekend(day):
            labels.append("周末")
        return "且".join(labels) if labels else "工作日"

    def adjust(self, day: date) -> tuple[date, str | None]:
        return roll_forward(self.jurisdiction, day)

    def generate_nodes(self, context: RuleContext) -> list[GeneratedNode]:
        nodes: list[GeneratedNode] = []
        for rule in self.rules:
            nodes.extend(rule.generate(context, self))
        nodes.sort(key=lambda node: (node.due_date, node.node_key))
        return nodes


@dataclass(frozen=True, slots=True)
class Rule:
    code: str
    kind: str
    title: str
    source: str
    generate: Callable[[RuleContext, JurisdictionCalendar], tuple[GeneratedNode, ...]]
    description: str = ""

    def catalog(self) -> dict:
        return {
            "rule_code": self.code,
            "kind": self.kind,
            "title": self.title,
            "source": self.source,
            "description": self.description,
        }


def fixed_deadline_rule(
    *,
    code: str,
    kind: str,
    title: str,
    source: str,
    anchor: str,
    offset_months: int,
    grace_months: int = 0,
    description: str = "",
) -> Rule:
    """锚点日 + 固定月数的一次性节点；可选宽限期（节点日期取宽限届满日）。"""

    def generate(ctx: RuleContext, calendar: JurisdictionCalendar) -> tuple[GeneratedNode, ...]:
        anchor_date = getattr(ctx, anchor)
        if anchor_date is None:
            return ()
        nominal = add_months(anchor_date, offset_months)
        explanation = (
            f"{title}：自{_ANCHOR_LABELS[anchor]} {anchor_date.isoformat()} 起 {offset_months} 个月，"
            f"法定期满日 {nominal.isoformat()}（{calendar.explain_day(nominal)}）"
        )
        _, nominal_roll = calendar.adjust(nominal)
        if nominal_roll:
            explanation += f"；{nominal_roll}"
        final_date = nominal
        if grace_months:
            final_date = add_months(nominal, grace_months)
            _, grace_roll = calendar.adjust(final_date)
            explanation += (
                f"；宽限期 {grace_months} 个月，宽限届满日 {final_date.isoformat()}"
                f"（{calendar.explain_day(final_date)}）"
            )
            if grace_roll:
                explanation += f"；{grace_roll}"
        due, _ = calendar.adjust(final_date)
        explanation += f"。最终节点日期 {due.isoformat()}。规则依据：{source}"
        return (
            GeneratedNode(
                node_key=f"{kind}:{anchor}",
                kind=kind,
                due_date=due,
                nominal_due_date=nominal,
                title=title,
                rule_code=code,
                rule_source=source,
                explanation=explanation,
            ),
        )

    return Rule(code=code, kind=kind, title=title, source=source, generate=generate, description=description)


def annuity_rule(
    *,
    code: str,
    source: str,
    anchor: str = "filing_date",
    years: Iterable[int],
    grace_months: int,
    due_on_anniversary_month_end: bool = False,
    normal_months_before_deadline: int = 0,
    year_titles: dict[int, str] | None = None,
    description: str = "",
) -> Rule:
    """逐年（或指定年度）年费 / 维持费规则。

    年度 ``year_no`` 的最终节点日期为锚点周年（可顺延到当月最后一日），
    另可加 ``grace_months`` 宽限期；若正常缴费窗口在最终节点之前
    （如美国维持费：正常截止 3.5 年、宽限截止 4 年），用
    ``normal_months_before_deadline`` 指回。``annuities_paid`` 之后
    的年度才会生成。
    """
    year_tuple = tuple(years)
    titles = year_titles or {}

    def deadline_date(anchor_date: date, year_no: int) -> date:
        anniversary = add_years(anchor_date, year_no)
        if not due_on_anniversary_month_end:
            return anniversary
        last_day = _calendar.monthrange(anniversary.year, anniversary.month)[1]
        return date(anniversary.year, anniversary.month, last_day)

    def generate(ctx: RuleContext, calendar: JurisdictionCalendar) -> tuple[GeneratedNode, ...]:
        anchor_date = getattr(ctx, anchor)
        if anchor_date is None:
            return ()
        nodes: list[GeneratedNode] = []
        for year_no in year_tuple:
            if year_no <= ctx.annuities_paid:
                continue
            deadline = deadline_date(anchor_date, year_no)
            if normal_months_before_deadline:
                nominal = add_months(deadline, -normal_months_before_deadline)
                grace_deadline = deadline
                grace_label = "加收附加费宽限"
            else:
                nominal = deadline
                grace_deadline = add_months(nominal, grace_months) if grace_months else nominal
                grace_label = "滞纳/宽限"
            due, _ = calendar.adjust(grace_deadline)
            title = titles.get(year_no, f"第 {year_no} 年度年费")
            explanation = (
                f"{title}：以{_ANCHOR_LABELS[anchor]}周年为缴费基准，正常缴费截止日 "
                f"{nominal.isoformat()}（{calendar.explain_day(nominal)}）"
            )
            _, nominal_roll = calendar.adjust(nominal)
            if nominal_roll:
                explanation += f"；{nominal_roll}"
            if grace_deadline != nominal:
                _, grace_roll = calendar.adjust(grace_deadline)
                explanation += (
                    f"；{grace_label}期至 {grace_deadline.isoformat()}"
                    f"（{calendar.explain_day(grace_deadline)}）"
                )
                if grace_roll:
                    explanation += f"；{grace_roll}"
            explanation += f"。最终节点日期 {due.isoformat()}。规则依据：{source}"
            nodes.append(
                GeneratedNode(
                    node_key=f"annuity:y{year_no}",
                    kind="annuity",
                    due_date=due,
                    nominal_due_date=nominal,
                    title=title,
                    rule_code=code,
                    rule_source=source,
                    explanation=explanation,
                    sequence_no=year_no,
                )
            )
        return tuple(nodes)

    return Rule(
        code=code,
        kind="annuity",
        title="年费/维持费",
        source=source,
        generate=generate,
        description=description,
    )


# ---------------------------------------------------------------------------
# 辖区注册
# ---------------------------------------------------------------------------

def _build_cn() -> JurisdictionCalendar:
    rules = (
        fixed_deadline_rule(
            code="CN-PCL-12M",
            kind="priority_claim",
            title="主张本国/外国优先权（12 个月）",
            source="《中华人民共和国专利法》第二十九条；《保护工业产权巴黎公约》第 4 条",
            anchor="filing_date",
            offset_months=12,
            description="自在先申请之日起十二个月内主张优先权",
        ),
        fixed_deadline_rule(
            code="CN-PUB-18M",
            kind="expected_publication",
            title="预期申请公布（18 个月）",
            source="《中华人民共和国专利法》第三十四条（自申请日/优先权日起满十八个月公布）",
            anchor="priority_date",
            offset_months=18,
            description="跟踪满十八个月公布的预期节点，日期以实际公开日补录为准",
        ),
        annuity_rule(
            code="CN-ANN",
            source="《中华人民共和国专利法》第四十三条；《专利法实施细则》第九十八条（六个月滞纳期）",
            anchor="filing_date",
            years=range(1, 21),
            grace_months=6,
            description="逐年缴纳年费，滞纳期六个月内补缴并承担滞纳金",
        ),
    )
    return JurisdictionCalendar("CN", JURISDICTION_TIMEZONES["CN"], rules, _HOLIDAYS["CN"])


def _build_us() -> JurisdictionCalendar:
    rules = (
        fixed_deadline_rule(
            code="US-PARIS-12M",
            kind="priority_claim",
            title="巴黎公约优先权（12 个月）",
            source="35 U.S.C. §119(a)；Paris Convention Article 4.C(1)",
            anchor="filing_date",
            offset_months=12,
            description="自首次外国申请之日起十二个月内主张优先权",
        ),
        fixed_deadline_rule(
            code="US-NP-30M",
            kind="national_phase",
            title="PCT 进入美国国家阶段（30 个月）",
            source="35 U.S.C. §371(b)；PCT Article 22(1)（最早优先权日起三十个月）",
            anchor="priority_date",
            offset_months=30,
            description="PCT 国际申请最早优先权日起三十个月届满前办理进入国家阶段手续",
        ),
        annuity_rule(
            code="US-MF",
            source="37 C.F.R. §1.362（维持费窗口为授权后 3–3.5/7–7.5/11–11.5 年；35 U.S.C. §41(b)(1) 六个月宽限期加收附加费）",
            anchor="grant_date",
            years=(4, 8, 12),
            grace_months=0,
            normal_months_before_deadline=6,
            year_titles={
                4: "第 3.5 年期维持费",
                8: "第 7.5 年期维持费",
                12: "第 11.5 年期维持费",
            },
            description="授权日为锚点：正常窗口截止于授权后 3.5/7.5/11.5 年，节点日期为六个月加收附加费宽限期届满日",
        ),
    )
    return JurisdictionCalendar("US", JURISDICTION_TIMEZONES["US"], rules, _HOLIDAYS["US"])


def _build_ep() -> JurisdictionCalendar:
    rules = (
        fixed_deadline_rule(
            code="EP-PARIS-12M",
            kind="priority_claim",
            title="巴黎公约优先权（12 个月）",
            source="EPC Article 87(1)；Paris Convention Article 4.C(1)",
            anchor="filing_date",
            offset_months=12,
            description="自首次申请之日起十二个月内主张优先权",
        ),
        fixed_deadline_rule(
            code="EP-NP-31M",
            kind="national_phase",
            title="PCT 进入欧洲地区阶段（31 个月）",
            source="EPC Rule 159(1)（最早优先权日起三十一个月）",
            anchor="priority_date",
            offset_months=31,
            description="PCT 国际申请最早优先权日起三十一个月届满前办理进入地区阶段手续",
        ),
        annuity_rule(
            code="EP-ANN",
            source="EPC Rule 51(1)(2)（第三年起逐年缴纳年费，以申请日所在月最后一日为到期日，另享六个月宽限期）",
            anchor="filing_date",
            years=range(3, 21),
            grace_months=6,
            due_on_anniversary_month_end=True,
            description="自第三年起逐年缴纳，到期日为周年所在月最后一日",
        ),
    )
    return JurisdictionCalendar("EP", JURISDICTION_TIMEZONES["EP"], rules, _HOLIDAYS["EP"])


REGISTRY: dict[str, JurisdictionCalendar] = {
    "CN": _build_cn(),
    "US": _build_us(),
    "EP": _build_ep(),
}


def supported_jurisdictions() -> list[str]:
    return sorted(REGISTRY)


def get_calendar(jurisdiction: str) -> JurisdictionCalendar:
    try:
        return REGISTRY[jurisdiction]
    except KeyError as exc:
        raise ValueError(f"不支持的辖区：{jurisdiction}") from exc
