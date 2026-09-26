from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.database import get_connection, transaction
from app.deadlines.schemas import (
    AnchorUpdate,
    ApplicationCreate,
    ConfirmRequest,
    ExtendRequest,
    PaymentRequest,
    ReopenRequest,
)
from app.deadlines.service import DeadlineService

router = APIRouter(prefix="/api/deadlines", tags=["专利期限"])


@router.post("/applications", status_code=status.HTTP_201_CREATED)
def create_application(payload: ApplicationCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return DeadlineService(connection).register_application(principal, payload.model_dump())


@router.get("/applications")
def list_applications(principal: Principal = Depends(current_principal)):
    return {"applications": DeadlineService(get_connection()).list_applications(principal)}


@router.get("/applications/{application_id}")
def get_application(application_id: int, principal: Principal = Depends(current_principal)):
    service = DeadlineService(get_connection())
    application = service._get_application(application_id)  # noqa: SLF001 - 读取后统一鉴权
    principal.require("deadlines.read")
    return {"application": application, "nodes": service.list_nodes(principal=principal, application_id=application_id)}


@router.patch("/applications/{application_id}/anchors")
def update_anchors(application_id: int, payload: AnchorUpdate, principal: Principal = Depends(current_principal)):
    data = payload.model_dump(exclude_unset=True)
    with transaction(immediate=True) as connection:
        return DeadlineService(connection).update_anchors(principal, application_id, data)


@router.post("/applications/{application_id}/recompute")
def recompute_application(application_id: int, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return DeadlineService(connection).recompute(principal, application_id)


@router.get("/applications/{application_id}/preview")
def preview_application(
    application_id: int,
    as_of: date = Query(..., description="按此辖区历法日重算状态，不写库"),
    principal: Principal = Depends(current_principal),
):
    return DeadlineService(get_connection()).preview_at(principal, application_id, as_of)


@router.get("/nodes")
def list_nodes(
    status_filter: str | None = Query(default=None, alias="status"),
    jurisdiction: str | None = None,
    application_id: int | None = None,
    as_of: date | None = Query(default=None, description="给定时间点的状态视图，不改变库中状态"),
    principal: Principal = Depends(current_principal),
):
    if status_filter and status_filter not in ("pending", "handled", "overdue"):
        from app.core.errors import ValidationError

        raise ValidationError("status 只能是 pending、handled 或 overdue")
    service = DeadlineService(get_connection())
    nodes = service.list_nodes(
        principal=principal,
        application_id=application_id,
        jurisdiction=jurisdiction,
        status=status_filter,
        as_of=as_of,
    )
    return {"as_of": as_of.isoformat() if as_of else None, "nodes": nodes}


@router.get("/nodes/{node_id}")
def get_node(node_id: int, principal: Principal = Depends(current_principal)):
    service = DeadlineService(get_connection())
    principal.require("deadlines.read")
    return service.get_node(node_id)


@router.get("/nodes/{node_id}/actions")
def list_node_actions(node_id: int, principal: Principal = Depends(current_principal)):
    service = DeadlineService(get_connection())
    return {"actions": service.list_actions(principal, node_id)}


@router.post("/nodes/{node_id}/confirm")
def confirm_node(node_id: int, payload: ConfirmRequest, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return DeadlineService(connection).confirm(principal, node_id, payload.note)


@router.post("/nodes/{node_id}/extend")
def extend_node(node_id: int, payload: ExtendRequest, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return DeadlineService(connection).extend(
            principal,
            node_id,
            payload.new_due_date,
            evidence_reference=payload.evidence_reference,
            note=payload.note,
        )


@router.post("/nodes/{node_id}/payment", status_code=status.HTTP_201_CREATED)
def record_payment(node_id: int, payload: PaymentRequest, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return DeadlineService(connection).record_payment(
            principal,
            node_id,
            evidence_reference=payload.evidence_reference,
            note=payload.note,
            paid_at=payload.paid_at,
        )


@router.post("/nodes/{node_id}/reopen")
def reopen_node(node_id: int, payload: ReopenRequest, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return DeadlineService(connection).reopen(principal, node_id, note=payload.note)


@router.get("/rules/{rule_code}")
def explain_rule(rule_code: str, principal: Principal = Depends(current_principal)):
    return DeadlineService(get_connection()).explain_rule(principal, rule_code)


@router.post("/recover")
def recover_reminders(principal: Principal = Depends(current_principal)):
    """服务重启后调用：恢复未完成节点的提醒排程，幂等。"""
    principal.require("jobs.run")
    with transaction(immediate=True) as connection:
        return DeadlineService(connection).recover_reminders()


@router.post("/dispatch")
def dispatch_reminders(principal: Principal = Depends(current_principal)):
    """领取并发送所有到期提醒（worker 入口，可重复安全调用）。"""
    principal.require("jobs.run")
    with transaction(immediate=True) as connection:
        return {"processed": DeadlineService(connection).dispatch_due_reminders(f"api:{principal.username}")}
