from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.core.clock import FrozenClock
from app.core.errors import ConflictError
from app.core.security import Principal
from app.database import transaction
from app.deadlines.service import DeadlineService

PRINCIPAL = Principal(
    user_id=1,
    username="admin",
    display_name="管理员",
    department_id=None,
    permissions=frozenset({"*"}),
    session_id=1,
)


def _service(connection, moment):
    return DeadlineService(connection, FrozenClock(moment))


def _register(service, code, jurisdiction, application_date, **extra):
    payload = {
        "case_code": code,
        "title": f"测试案件-{code}",
        "jurisdiction": jurisdiction,
        "application_number": f"APP-{code}",
        "application_date": application_date,
    }
    payload.update(extra)
    return service.register_case(PRINCIPAL, payload)


def _register_case(client, admin, **overrides):
    payload = {
        "case_code": "CASE-API-001",
        "title": "高价值发明专利",
        "jurisdiction": "CN",
        "application_number": "CN202410001",
        "application_date": "2024-03-15",
        "priority_date": "2023-03-20",
    }
    payload.update(overrides)
    response = client.post("/api/deadlines/cases", headers=admin["headers"], json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_register_case_generates_explainable_nodes(client, admin):
    case = _register_case(client, admin)
    assert case["generation"]["created"] == 21
    detail = client.get(f"/api/deadlines/cases/{case['id']}", headers=admin["headers"])
    nodes = detail.json()["nodes"]
    assert len(nodes) == 21
    publication = next(n for n in nodes if n["kind"] == "publication")
    assert publication["due_date"] == "2024-09-20"
    assert publication["display_status"] == "已处理" or publication["display_status"] in ("待确认", "逾期")
    explain = client.get(f"/api/deadlines/nodes/{publication['id']}/explain", headers=admin["headers"])
    body = explain.json()
    assert "第三十四条" in body["explanation"]["legal_basis"]
    assert body["explanation"]["anchor"] == {"field": "earliest_priority", "date": "2023-03-20"}
    assert body["explanation"]["timezone"] == "Asia/Shanghai"
    assert "当地日期" in body["explanation"]["overdue_rule"]
    assert body["rule"] == {"code": "CN.publication", "version": 1}


def test_generation_is_idempotent(client, admin):
    case = _register_case(client, admin)
    again = client.post(f"/api/deadlines/cases/{case['id']}/regenerate", headers=admin["headers"])
    assert again.json()["created"] == 0
    assert again.json()["unchanged"] == 21
    recompute = client.post("/api/deadlines/recompute", headers=admin["headers"], json={})
    assert recompute.json()["totals"]["created"] == 0
    assert recompute.json()["totals"]["unchanged"] == 21
    detail = client.get(f"/api/deadlines/cases/{case['id']}", headers=admin["headers"]).json()
    keys = [n["node_key"] for n in detail["nodes"]]
    assert len(keys) == len(set(keys)) == 21


def test_holiday_and_weekend_adjustment(client, admin):
    with transaction(immediate=True) as connection:
        service = _service(connection, datetime(2026, 9, 1, tzinfo=UTC))
        case = _register(service, "CN-HOL", "CN", date(2025, 10, 1))
        nodes = service.list_nodes(PRINCIPAL, case_id=case["id"])["items"]
        first = next(n for n in nodes if n["kind"] == "annuity" and n["occurrence"] == 1)
        assert first["original_due_date"] == "2026-10-01"
        assert first["due_date"] == "2026-10-08"
        adjustment = first["explanation"]["holiday_adjustment"]
        assert adjustment["applied"] is True
        assert "2026-10-01" in adjustment["skipped_dates"]
        assert first["explanation"]["reminder"]["remind_date"] == "2026-09-08"

        weekend_case = _register(service, "CN-WKD", "CN", date(2025, 9, 26))
        weekend_nodes = service.list_nodes(PRINCIPAL, case_id=weekend_case["id"])["items"]
        node = next(n for n in weekend_nodes if n["kind"] == "annuity" and n["occurrence"] == 1)
        assert node["original_due_date"] == "2026-09-26"
        assert node["due_date"] == "2026-09-28"

        shift_case = _register(service, "CN-REM", "CN", date(2026, 1, 10))
        shift_nodes = service.list_nodes(PRINCIPAL, case_id=shift_case["id"])["items"]
        reminder = next(n for n in shift_nodes if n["kind"] == "annuity" and n["occurrence"] == 1)
        assert reminder["due_date"] == "2027-01-11"
        assert reminder["explanation"]["reminder"]["remind_date"] == "2026-12-11"
        assert reminder["explanation"]["reminder"]["shifted_earlier_for_non_working_day"] is True


def test_extension_is_recorded_and_survives_recompute(client, admin):
    case = _register_case(client, admin, priority_date=None)
    nodes = client.get(f"/api/deadlines/cases/{case['id']}", headers=admin["headers"]).json()["nodes"]
    annuity = next(n for n in nodes if n["kind"] == "annuity" and n["occurrence"] == 1)
    assert annuity["due_date"] == "2025-03-17"
    extended = client.post(
        f"/api/deadlines/nodes/{annuity['id']}/extensions",
        headers=admin["headers"],
        json={"days": 15, "reason": "收到官方延期通知"},
    )
    assert extended.status_code == 200, extended.text
    assert extended.json()["due_date"] == "2025-04-01"
    assert extended.json()["extension_days"] == 15
    regenerated = client.post(f"/api/deadlines/cases/{case['id']}/regenerate", headers=admin["headers"])
    assert regenerated.status_code == 200
    after = client.get(f"/api/deadlines/nodes/{annuity['id']}", headers=admin["headers"]).json()
    assert after["due_date"] == "2025-04-01"
    assert after["extension_days"] == 15
    assert [a["adjustment_type"] for a in after["adjustments"]] == ["extension"]
    assert after["adjustments"][0]["reason"] == "收到官方延期通知"


def test_payment_closes_node_and_old_tasks_cannot_reopen(client, admin):
    case = _register_case(client, admin, priority_date=None)
    nodes = client.get(f"/api/deadlines/cases/{case['id']}", headers=admin["headers"]).json()["nodes"]
    annuity = next(n for n in nodes if n["kind"] == "annuity" and n["occurrence"] == 1)
    paid = client.post(
        f"/api/deadlines/nodes/{annuity['id']}/payments",
        headers=admin["headers"],
        json={"voucher_no": "PAY-2025-0001", "paid_at": "2025-03-10", "note": "第2年度年费"},
    )
    assert paid.status_code == 200, paid.text
    assert paid.json()["status"] == "done"
    assert paid.json()["display_status"] == "已处理"
    assert paid.json()["payment_voucher"] == "PAY-2025-0001"

    regenerated = client.post(f"/api/deadlines/cases/{case['id']}/regenerate", headers=admin["headers"]).json()
    assert regenerated["preserved_closed"] >= 1
    still_done = client.get(f"/api/deadlines/nodes/{annuity['id']}", headers=admin["headers"]).json()
    assert still_done["status"] == "done"

    again = client.post(
        f"/api/deadlines/nodes/{annuity['id']}/payments",
        headers=admin["headers"],
        json={"voucher_no": "PAY-2025-0002", "paid_at": "2025-03-11"},
    )
    assert again.status_code == 409
    extend = client.post(
        f"/api/deadlines/nodes/{annuity['id']}/extensions",
        headers=admin["headers"],
        json={"days": 10, "reason": "不应生效"},
    )
    assert extend.status_code == 409
    complete = client.post(
        f"/api/deadlines/nodes/{annuity['id']}/complete",
        headers=admin["headers"],
        json={"worker_label": "法务甲", "resolution": "旧任务重复提交"},
    )
    assert complete.status_code == 409


def test_claim_complete_and_lease_recovery(client, admin):
    with transaction(immediate=True) as connection:
        service = _service(connection, datetime(2026, 12, 20, tzinfo=UTC))
        _register(service, "CN-CLAIM", "CN", date(2026, 1, 10))
        claimed = service.claim(PRINCIPAL, "法务甲", lease_seconds=60)
        assert claimed is not None
        assert claimed["kind"] == "annuity"
        assert claimed["status"] == "claimed"
        assert service.claim(PRINCIPAL, "法务乙", lease_seconds=60) is None
        with pytest.raises(ConflictError):
            service.complete(PRINCIPAL, claimed["id"], "法务乙", "越权办结")
        # 租约过期后（等同于服务重启），节点重新可领取
        restarted = _service(connection, datetime(2026, 12, 20, 0, 2, 1, tzinfo=UTC))
        reclaimed = restarted.claim(PRINCIPAL, "法务乙", lease_seconds=60)
        assert reclaimed is not None and reclaimed["id"] == claimed["id"]
        assert reclaimed["claimed_by"] == "法务乙"
        with pytest.raises(ConflictError):
            restarted.complete(PRINCIPAL, claimed["id"], "法务甲", "旧任务迟到提交")
        done = restarted.complete(PRINCIPAL, claimed["id"], "法务乙", "已缴纳第2年度年费")
        assert done["status"] == "done"
        assert done["display_status"] == "已处理"
        with pytest.raises(ConflictError):
            restarted.complete(PRINCIPAL, claimed["id"], "法务乙", "重复提交")


def test_release_returns_node_to_queue(client, admin):
    with transaction(immediate=True) as connection:
        service = _service(connection, datetime(2026, 12, 20, tzinfo=UTC))
        _register(service, "CN-REL", "CN", date(2026, 1, 10))
        claimed = service.claim(PRINCIPAL, "法务甲", lease_seconds=3600)
        released = service.release(PRINCIPAL, claimed["id"], "法务甲")
        assert released["status"] == "pending"
        again = service.claim(PRINCIPAL, "法务乙", lease_seconds=3600)
        assert again["id"] == claimed["id"]


def test_overdue_uses_jurisdiction_local_date(client, admin):
    with transaction(immediate=True) as connection:
        service = _service(connection, datetime(2026, 9, 20, tzinfo=UTC))
        jp_case = _register(service, "JP-TZ", "JP", date(2025, 3, 26))
        us_case = _register(service, "US-TZ", "US", date(2025, 3, 26))
        jp_node = service.list_nodes(PRINCIPAL, case_id=jp_case["id"])["items"][0]
        us_node = service.list_nodes(PRINCIPAL, case_id=us_case["id"])["items"][0]
        assert jp_node["due_date"] == "2026-09-28"  # 2026-09-26 为周六，顺延至周一
        assert us_node["due_date"] == "2026-09-28"

        def status_at(case_id, moment):
            items = service.list_nodes(PRINCIPAL, case_id=case_id, as_of=moment)["items"]
            return items[0]["display_status"]

        # 同一 UTC 时刻：东京已进入 9 月 29 日，纽约仍是 9 月 28 日
        instant = datetime(2026, 9, 29, 2, 0, tzinfo=UTC)
        assert status_at(jp_case["id"], instant) == "逾期"
        assert status_at(us_case["id"], instant) == "待确认"
        # 截止日当天最后一秒不逾期，次日零时起逾期（东京 UTC+9）
        assert status_at(jp_case["id"], datetime(2026, 9, 28, 14, 59, 59, tzinfo=UTC)) == "待确认"
        assert status_at(jp_case["id"], datetime(2026, 9, 28, 15, 0, 0, tzinfo=UTC)) == "逾期"
        # 纽约夏令时 UTC-4，同一规则
        assert status_at(us_case["id"], datetime(2026, 9, 29, 3, 59, 59, tzinfo=UTC)) == "待确认"
        assert status_at(us_case["id"], datetime(2026, 9, 29, 4, 0, 0, tzinfo=UTC)) == "逾期"


def test_list_nodes_recomputes_status_at_any_instant(client, admin):
    case = _register_case(client, admin, application_date="2020-06-15", priority_date=None)
    now_items = client.get(f"/api/deadlines/nodes?case_id={case['id']}", headers=admin["headers"]).json()["items"]
    overdue = [n for n in now_items if n["display_status"] == "逾期"]
    upcoming = [n for n in now_items if n["display_status"] == "待确认"]
    assert overdue and upcoming
    past = client.get(
        f"/api/deadlines/nodes?case_id={case['id']}&as_of=2020-01-01T00:00:00Z",
        headers=admin["headers"],
    ).json()["items"]
    assert past and all(n["display_status"] == "待确认" for n in past)
    filtered = client.get(
        f"/api/deadlines/nodes?case_id={case['id']}&display_status=逾期",
        headers=admin["headers"],
    ).json()["items"]
    assert len(filtered) == len(overdue)


def test_claim_and_complete_via_api(client, admin):
    case = _register_case(client, admin, application_date="2020-01-15", priority_date=None)
    claimed = client.post(
        "/api/deadlines/nodes/claim", headers=admin["headers"], json={"worker_label": "法务甲", "lease_seconds": 600}
    )
    assert claimed.status_code == 200, claimed.text
    node = claimed.json()["claimed"]
    assert node is not None and node["status"] == "claimed"
    assert node["case_id"] == case["id"]
    completed = client.post(
        f"/api/deadlines/nodes/{node['id']}/complete",
        headers=admin["headers"],
        json={"worker_label": "法务甲", "resolution": "第2年度年费已缴"},
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "done"
    repeat = client.post(
        f"/api/deadlines/nodes/{node['id']}/complete",
        headers=admin["headers"],
        json={"worker_label": "法务甲", "resolution": "旧任务重复提交"},
    )
    assert repeat.status_code == 409


def test_rules_registry_is_exposed(client, admin):
    body = client.get("/api/deadlines/rules", headers=admin["headers"]).json()
    assert body["jurisdictions"]["CN"] == "Asia/Shanghai"
    assert any(rule["code"] == "CN.annuity" for rule in body["rules"])


def test_rejects_unsupported_jurisdiction(client, admin):
    response = client.post(
        "/api/deadlines/cases",
        headers=admin["headers"],
        json={
            "case_code": "CASE-XX",
            "title": "测试案件",
            "jurisdiction": "XX",
            "application_number": "XX-1",
            "application_date": "2024-01-01",
        },
    )
    assert response.status_code == 422


def test_requires_authentication(client):
    assert client.get("/api/deadlines/nodes").status_code == 401
    assert client.post("/api/deadlines/cases", json={}).status_code == 401
