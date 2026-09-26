from __future__ import annotations

from datetime import date


def _create_application(client, headers, code="PAT-CN-WEB-1", **overrides):
    payload = {
        "application_code": code,
        "jurisdiction": "CN",
        "application_number": "CN2026100WEB",
        "title": "接口联调发明专利",
        "filing_date": "2025-10-01",
        "priority_date": "2025-10-01",
        "lead_days": [30, 7, 0],
    }
    payload.update(overrides)
    response = client.post("/api/deadlines/applications", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def test_deadline_full_flow_over_http(client, admin):
    headers = admin["headers"]

    created = _create_application(client, headers)
    application_id = created["application"]["id"]
    assert created["replayed"] is False
    priority_node = next(n for n in created["nodes"] if n["kind"] == "priority_claim")
    # 法定日 2026-10-01 国庆假日 → 顺延
    assert priority_node["due_date"] == "2026-10-09"
    assert "专利法" in priority_node["rule_source"]

    # 同一申请重复提交：幂等，不新增
    replayed = _create_application(client, headers)
    assert replayed["replayed"] is True
    assert len(replayed["nodes"]) == len(created["nodes"])

    # 按未来时间点查看：出现逾期节点但库内状态不变
    future = client.get(
        f"/api/deadlines/applications/{application_id}/preview?as_of=2030-01-01",
        headers=headers,
    )
    assert future.status_code == 200
    assert any(n["status_as_of"] == "overdue" for n in future.json()["nodes"])

    overdue = client.get("/api/deadlines/nodes?as_of=2030-01-01&status=overdue", headers=headers)
    assert overdue.status_code == 200
    assert overdue.json()["nodes"], "未来视图应包含逾期节点"

    current = client.get("/api/deadlines/nodes?status=overdue", headers=headers)
    assert current.status_code == 200
    assert current.json()["nodes"] == [], "当前没有真正逾期的节点"

    # 登记确认 → 延期 → 缴费
    node_id = priority_node["id"]
    confirm = client.post(f"/api/deadlines/nodes/{node_id}/confirm", json={"note": "已电话确认"}, headers=headers)
    assert confirm.status_code == 200
    assert confirm.json()["confirmed_at"]

    extend = client.post(
        f"/api/deadlines/nodes/{node_id}/extend",
        json={"new_due_date": "2026-12-31", "evidence_reference": "CNIPA-EXT-9", "note": "获准顺延"},
        headers=headers,
    )
    assert extend.status_code == 200, extend.text
    assert extend.json()["due_date"] == "2026-12-31"

    # 延期不能早于当前期满日
    bad = client.post(
        f"/api/deadlines/nodes/{node_id}/extend",
        json={"new_due_date": "2026-11-01"},
        headers=headers,
    )
    assert bad.status_code == 422

    payment = client.post(
        f"/api/deadlines/nodes/{node_id}/payment",
        json={"evidence_reference": "RCPT-WEB-0001", "note": "电子回单"},
        headers=headers,
    )
    assert payment.status_code == 201, payment.text
    assert payment.json()["status"] == "handled"

    # 缴费凭证必填
    payment_bad = client.post(
        f"/api/deadlines/nodes/{node_id}/payment",
        json={"evidence_reference": "x"},
        headers=headers,
    )
    assert payment_bad.status_code == 422

    # 动作流水
    actions = client.get(f"/api/deadlines/nodes/{node_id}/actions", headers=headers)
    assert [a["action_type"] for a in actions.json()["actions"]] == ["confirm", "extend", "payment"]

    # 显式重开
    reopen = client.post(f"/api/deadlines/nodes/{node_id}/reopen", json={"note": "回单冲正"}, headers=headers)
    assert reopen.status_code == 200
    assert reopen.json()["status"] == "pending"


def test_recompute_after_grant_date_creates_maintenance_fees(client, admin):
    headers = admin["headers"]
    created = _create_application(
        client,
        headers,
        code="PAT-US-WEB-1",
        jurisdiction="US",
        application_number="US17/999WEB",
        filing_date="2026-06-01",
        priority_date="2026-06-01",
    )
    app_id = created["application"]["id"]
    assert not [n for n in created["nodes"] if n["kind"] == "annuity"]

    patched = client.patch(
        f"/api/deadlines/applications/{app_id}/anchors",
        json={"grant_date": "2028-01-15"},
        headers=headers,
    )
    assert patched.status_code == 200, patched.text
    maintenance = [n for n in patched.json()["nodes"] if n["kind"] == "annuity"]
    assert {n["sequence_no"] for n in maintenance} == {4, 8, 12}


def test_recover_and_dispatch_endpoints_are_idempotent(client, admin):
    headers = admin["headers"]
    _create_application(
        client,
        headers,
        code="PAT-CN-WEB-FAR",
        application_number="CN2030100FAR",
        filing_date="2030-06-01",
        priority_date="2030-06-01",
    )

    first = client.post("/api/deadlines/recover", headers=headers)
    assert first.status_code == 200
    second = client.post("/api/deadlines/recover", headers=headers)
    assert second.json() == first.json()

    # 所有提醒均在未来，当前没有到期任务可领取
    dispatched = client.post("/api/deadlines/dispatch", headers=headers)
    assert dispatched.status_code == 200
    assert dispatched.json()["processed"] == 0


def test_rule_explanation_endpoint(client, admin):
    response = client.get("/api/deadlines/rules/EP-NP-31M", headers=admin["headers"])
    assert response.status_code == 200
    body = response.json()
    assert body["jurisdiction"] == "EP"
    assert "EPC Rule 159" in body["source"]

    assert client.get("/api/deadlines/rules/NOPE", headers=admin["headers"]).status_code == 404


def test_deadline_endpoints_require_authentication(client):
    assert client.get("/api/deadlines/nodes").status_code == 401
    assert client.post("/api/deadlines/applications", json={}).status_code == 401
