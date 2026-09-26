from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.core.clock import FrozenClock
from app.database import transaction
from app.deadlines.rules import (
    add_months,
    get_calendar,
    local_today,
    roll_forward,
)
from app.deadlines.service import DeadlineService


class _Principal:
    def __init__(self, user_id: int | None = None, name: str = "专利管理员") -> None:
        self.user_id = user_id
        self.username = "manager"
        self.display_name = name
        self.permissions = frozenset({"*"})

    def require(self, permission: str) -> None:
        return None


PRINCIPAL = _Principal()
NOW = datetime(2026, 9, 26, 2, 0, tzinfo=UTC)


@pytest.fixture()
def service(client):
    clock = FrozenClock(NOW)
    with transaction(immediate=True) as connection:
        yield DeadlineService(connection, clock)


def _cn_application(**overrides):
    data = {
        "application_code": "PAT-CN-0001",
        "jurisdiction": "CN",
        "application_number": "CN2026100001",
        "title": "示例发明专利",
        "filing_date": date(2026, 6, 1),
        "priority_date": date(2026, 6, 1),
        "lead_days": [30, 7, 0],
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# 规则引擎
# ---------------------------------------------------------------------------

def test_add_months_clamps_to_month_end():
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
    assert add_months(date(2026, 6, 1), 12) == date(2027, 6, 1)


def test_holiday_rolls_forward_to_next_business_day():
    # 2026-10-01 至 10-08 为中国国庆假期段（含周末），09 日恢复上班
    due, reason = roll_forward("CN", date(2026, 10, 1))
    assert due == date(2026, 10, 9)
    assert "顺延" in reason
    assert roll_forward("CN", date(2026, 10, 9)) == (date(2026, 10, 9), None)


def test_local_today_resolves_per_jurisdiction():
    moment = datetime(2026, 9, 26, 1, 0, tzinfo=UTC)
    assert local_today(moment, "Asia/Shanghai") == date(2026, 9, 26)
    assert local_today(moment, "America/New_York") == date(2026, 9, 25)


def test_cn_priority_and_annuity_nodes_carry_rule_source():
    calendar = get_calendar("CN")
    from app.deadlines.rules import RuleContext

    nodes = calendar.generate_nodes(
        RuleContext(filing_date=date(2025, 10, 1), priority_date=date(2025, 10, 1))
    )
    priority = next(n for n in nodes if n.kind == "priority_claim")
    # 法定日落在国庆假日，顺延到 10 月 9 日
    assert priority.nominal_due_date == date(2026, 10, 1)
    assert priority.due_date == date(2026, 10, 9)
    assert "专利法" in priority.rule_source
    assert "顺延" in priority.explanation

    annuity = next(n for n in nodes if n.kind == "annuity" and n.sequence_no == 1)
    assert annuity.nominal_due_date == date(2026, 10, 1)
    # 六个月滞纳期至 2027-04-01
    assert annuity.due_date == date(2027, 4, 1)


def test_us_maintenance_fee_uses_grant_anchor_and_surcharge_window():
    calendar = get_calendar("US")
    from app.deadlines.rules import RuleContext

    nodes = calendar.generate_nodes(
        RuleContext(filing_date=date(2026, 6, 1), grant_date=date(2028, 1, 1))
    )
    maintenance = {(n.sequence_no): n for n in nodes if n.kind == "annuity"}
    assert set(maintenance) == {4, 8, 12}
    assert maintenance[4].nominal_due_date == date(2031, 7, 1)
    assert maintenance[4].due_date == date(2032, 1, 1)
    # 授权日未知时不生成维持费节点
    assert not [n for n in calendar.generate_nodes(RuleContext(filing_date=date(2026, 6, 1))) if n.kind == "annuity"]


# ---------------------------------------------------------------------------
# 注册 / 幂等 / 节点生成
# ---------------------------------------------------------------------------

def test_register_generates_nodes_and_schedules_each_reminder_once(service):
    result = service.register_application(PRINCIPAL, _cn_application())
    assert result["replayed"] is False
    kinds = {n["kind"] for n in result["nodes"]}
    assert {"priority_claim", "expected_publication", "annuity"} <= kinds

    jobs = service.connection.execute(
        "SELECT COUNT(*) AS c FROM background_jobs WHERE job_type='deadline.reminder' AND status='pending'"
    ).fetchone()["c"]
    dispatches = service.connection.execute("SELECT COUNT(*) AS c FROM deadline_reminder_dispatches").fetchone()["c"]
    assert jobs == dispatches
    assert jobs == len(result["nodes"]) * 3

    # 重复登记同一申请：不重建任何节点或提醒
    again = service.register_application(PRINCIPAL, _cn_application())
    assert again["replayed"] is True
    assert len(again["nodes"]) == len(result["nodes"])
    assert service.connection.execute("SELECT COUNT(*) AS c FROM deadline_nodes").fetchone()["c"] == len(result["nodes"])
    assert service.connection.execute("SELECT COUNT(*) AS c FROM background_jobs WHERE job_type='deadline.reminder'").fetchone()["c"] == jobs


def test_recover_does_not_duplicate_and_rebuilds_cancelled(service):
    service.register_application(PRINCIPAL, _cn_application())
    before = service.connection.execute("SELECT COUNT(*) AS c FROM background_jobs WHERE job_type='deadline.reminder'").fetchone()["c"]
    assert service.recover_reminders()["open_nodes"] > 0
    assert service.connection.execute("SELECT COUNT(*) AS c FROM background_jobs WHERE job_type='deadline.reminder'").fetchone()["c"] == before

    # 取消一条未发送的任务后恢复：自动以新一代排程补齐
    row = service.connection.execute(
        "SELECT id FROM deadline_reminder_dispatches WHERE dispatched_at IS NULL LIMIT 1"
    ).fetchone()
    service.connection.execute("UPDATE background_jobs SET status='cancelled' WHERE id=?", (row["id"],))
    service.recover_reminders()
    pending = service.connection.execute(
        "SELECT COUNT(*) AS c FROM deadline_reminder_dispatches d "
        "JOIN background_jobs j ON j.id=d.job_id WHERE j.status='pending'"
    ).fetchone()["c"]
    total_dispatches = service.connection.execute("SELECT COUNT(*) AS c FROM deadline_reminder_dispatches").fetchone()["c"]
    assert pending == total_dispatches


# ---------------------------------------------------------------------------
# 逾期判定与跨时区
# ---------------------------------------------------------------------------

def test_same_calendar_day_is_never_overdue_across_timezones(service):
    service.register_application(PRINCIPAL, _us_application())
    node = service.connection.execute(
        "SELECT * FROM deadline_nodes WHERE kind='priority_claim'"
    ).fetchone()
    due = node["due_date"]  # 2027-06-01，辖区 America/New_York
    assert due == "2027-06-01"

    # UTC 已进入 6 月 2 日凌晨，但纽约仍是 6 月 1 日晚上 → 不逾期
    assert service.sweep_overdue(moment=datetime(2027, 6, 2, 0, 30, tzinfo=UTC)) == 0
    # 纽约本地进入 6 月 2 日（UTC 6 月 2 日 04:00）→ 逾期
    assert service.sweep_overdue(moment=datetime(2027, 6, 2, 4, 0, tzinfo=UTC)) == 1
    status = service.connection.execute("SELECT status FROM deadline_nodes WHERE id=?", (node["id"],)).fetchone()["status"]
    assert status == "overdue"


def test_china_timezone_day_boundary(service):
    service.register_application(
        PRINCIPAL,
        _cn_application(
            filing_date=date(2026, 9, 24),
            priority_date=date(2026, 9, 24),
        ),
    )
    # 优先权节点：2027-09-24（周五，工作日）
    node = service.connection.execute(
        "SELECT * FROM deadline_nodes WHERE kind='priority_claim'"
    ).fetchone()
    assert node["due_date"] == "2027-09-24"
    # UTC 2027-09-24 15:59 = 上海 23:59 同一天 → 不逾期
    assert service.sweep_overdue(moment=datetime(2027, 9, 24, 15, 59, tzinfo=UTC)) == 0
    # UTC 16:01 = 上海 2027-09-25 00:01 → 期满次日才逾期
    assert service.sweep_overdue(moment=datetime(2027, 9, 24, 16, 1, tzinfo=UTC)) == 1
    assert service.get_node(node["id"])["status"] == "overdue"


def _us_application():
    return {
        "application_code": "PAT-US-0001",
        "jurisdiction": "US",
        "application_number": "US17/000001",
        "filing_date": date(2026, 6, 1),
        "priority_date": date(2026, 6, 1),
        "lead_days": [30, 0],
    }


# ---------------------------------------------------------------------------
# 待确认 / 已处理 / 逾期 状态机
# ---------------------------------------------------------------------------

def test_confirm_extend_payment_and_reopen_lifecycle(service):
    service.register_application(PRINCIPAL, _cn_application())
    node = service.list_nodes(application_id=1)[0]

    # 待确认：登记确认后仍是 pending，但保留确认痕迹
    service.confirm(PRINCIPAL, node["id"], note="已通知承办人")
    confirmed = service.get_node(node["id"])
    assert confirmed["status"] == "pending"
    assert confirmed["confirmed_at"]

    # 延期：期满日必须更晚，旧任务取消、新代次任务建立
    pending_before = service.connection.execute(
        "SELECT COUNT(*) AS c FROM background_jobs WHERE job_type='deadline.reminder' AND status='pending'"
    ).fetchone()["c"]
    new_due = date.fromisoformat(node["due_date"]).replace(year=date.fromisoformat(node["due_date"]).year + 1)
    extended = service.extend(PRINCIPAL, node["id"], new_due, evidence_reference="USPTO-EXT-1", note="获准延期")
    assert extended["due_date"] == new_due.isoformat()
    assert extended["extension_count"] == 1
    pending_after = service.connection.execute(
        "SELECT COUNT(*) AS c FROM background_jobs WHERE job_type='deadline.reminder' AND status='pending'"
    ).fetchone()["c"]
    assert pending_after == pending_before
    generations = {r["schedule_generation"] for r in service.connection.execute(
        "SELECT DISTINCT schedule_generation FROM deadline_reminder_dispatches WHERE node_id=?",
        (node["id"],),
    ).fetchall()}
    assert generations == {1}

    # 缴费凭证登记：节点已处理，未完成提醒被取消
    paid = service.record_payment(PRINCIPAL, node["id"], evidence_reference="RCPT-2027-0001", note="银行回单")
    assert paid["status"] == "handled"
    assert paid["handled_at"]
    pending_left = service.connection.execute(
        "SELECT COUNT(*) AS c FROM deadline_reminder_dispatches d "
        "JOIN background_jobs j ON j.id=d.job_id WHERE d.node_id=? AND j.status='pending'",
        (node["id"],),
    ).fetchone()["c"]
    assert pending_left == 0

    # 已处理节点不能直接延期
    from app.core.errors import ConflictError

    with pytest.raises(ConflictError):
        service.extend(PRINCIPAL, node["id"], new_due.replace(year=new_due.year + 1))

    # 动作流水完整
    actions = service.list_actions(PRINCIPAL, node["id"])
    assert [a["action_type"] for a in actions] == ["confirm", "extend", "payment"]


def test_handled_node_is_not_reopened_by_recompute_or_old_jobs(service):
    service.register_application(
        PRINCIPAL,
        _cn_application(
            filing_date=date(2025, 10, 1),
            priority_date=date(2025, 10, 1),
        ),
    )
    node = service.connection.execute(
        "SELECT * FROM deadline_nodes WHERE kind='priority_claim'"
    ).fetchone()
    service.record_payment(PRINCIPAL, node["id"], evidence_reference="RCPT-X-1")

    # 重算（模拟规则表更新后再跑）不改变已处理节点
    result = service.recompute(PRINCIPAL, 1)
    assert result["nodes_changed"]["updated"] == 0
    assert service.get_node(node["id"])["status"] == "handled"

    # 旧排程任务即使被 worker 领取执行，也只会空跑
    old_job = service.connection.execute(
        "SELECT j.* FROM background_jobs j JOIN deadline_reminder_dispatches d ON d.job_id=j.id "
        "WHERE d.node_id=? LIMIT 1",
        (node["id"],),
    ).fetchone()
    outcome = service._deliver_reminder(dict(old_job))
    assert outcome["delivered"] is False
    assert outcome["reason"] == "already_handled"
    assert service.get_node(node["id"])["status"] == "handled"


def test_payment_advances_annuity_progress(service):
    service.register_application(PRINCIPAL, _cn_application())
    first = service.connection.execute(
        "SELECT * FROM deadline_nodes WHERE kind='annuity' ORDER BY due_date LIMIT 1"
    ).fetchone()
    service.record_payment(PRINCIPAL, first["id"], evidence_reference="RCPT-ANN-1")
    app = service.connection.execute("SELECT annuities_paid FROM patent_applications WHERE id=1").fetchone()
    assert app["annuities_paid"] == 1
    # 第一年节点已处理，其余年度仍为待处理
    assert service.get_node(first["id"])["status"] == "handled"


# ---------------------------------------------------------------------------
# 任意时间点重算
# ---------------------------------------------------------------------------

def test_preview_as_of_explains_without_writing(service):
    service.register_application(PRINCIPAL, _cn_application())
    future = service.preview_at(PRINCIPAL, 1, date(2030, 1, 1))
    assert future["rules_version"]
    overdue_keys = {n["node_key"] for n in future["nodes"] if n["status_as_of"] == "overdue"}
    assert "priority_claim:filing_date" in overdue_keys
    # 预览不写库
    persisted = service.list_nodes(application_id=1)
    assert all(n["effective_status"] == "pending" for n in persisted)

    # 规则解释可追溯到条款来源
    rule = service.explain_rule(PRINCIPAL, "CN-PCL-12M")
    assert rule["jurisdiction"] == "CN"
    assert "巴黎公约" in rule["source"]


# ---------------------------------------------------------------------------
# 可恢复的任务领取
# ---------------------------------------------------------------------------

def test_claimed_job_returns_to_queue_after_lease_expiry(service):
    service.register_application(
        PRINCIPAL,
        _cn_application(
            filing_date=date(2026, 8, 1),
            priority_date=date(2026, 8, 1),
            lead_days=[300],
        ),
    )
    # 只让其中一条任务到期可领取
    job_id = service.connection.execute(
        "SELECT id FROM background_jobs WHERE job_type='deadline.reminder' LIMIT 1"
    ).fetchone()["id"]
    service.connection.execute(
        "UPDATE background_jobs SET available_at=? WHERE id=?",
        ("2026-09-20T09:00:00+00:00", job_id),
    )
    claimed = service.jobs.claim("worker-a", lease_seconds=60, job_type="deadline.reminder")
    assert claimed["id"] == job_id
    assert claimed["status"] == "running"
    # 同一时刻其他 worker 领不到
    assert service.jobs.claim("worker-b", lease_seconds=60, job_type="deadline.reminder") is None
    # 模拟崩溃：时钟越过租约后任务重新回到待领取，由 worker 恢复处理
    service.clock.advance(seconds=61)
    processed = service.dispatch_due_reminders("worker-b")
    assert processed == 1
    assert service.connection.execute(
        "SELECT status FROM background_jobs WHERE id=?", (job_id,)
    ).fetchone()["status"] == "completed"
