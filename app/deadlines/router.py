from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.database import get_connection, transaction
from app.deadlines.calendars import JURISDICTION_TIMEZONES
from app.deadlines.rules import RULE_VERSION, RULES
from app.deadlines.schemas import (
    CaseCreate,
    ClaimRequest,
    CompleteRequest,
    ExtensionCreate,
    PaymentCreate,
    ReleaseRequest,
    RecomputeRequest,
)
from app.deadlines.service import DeadlineService

router = APIRouter(prefix="/api/deadlines", tags=["专利期限"])


@router.get("/rules")
def list_rules(principal: Principal = Depends(current_principal)) -> dict:
    principal.require("deadlines.read")
    return {
        "rule_version": RULE_VERSION,
        "jurisdictions": JURISDICTION_TIMEZONES,
        "rules": [
            {
                "code": rule.code,
                "jurisdiction": rule.jurisdiction,
                "kind": rule.kind,
                "kind_label": rule.kind_label,
                "anchor": rule.anchor,
                "offset_months": rule.offset_months,
                "recurrence_months": rule.recurrence_months,
                "max_occurrences": rule.max_occurrences,
                "lead_days": rule.lead_days,
                "legal_basis": rule.legal_basis,
            }
            for rule in RULES
        ],
    }


@router.post("/cases", status_code=status.HTTP_201_CREATED)
def register_case(payload: CaseCreate, principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return DeadlineService(connection).register_case(principal, payload.model_dump())


@router.get("/cases")
def list_cases(jurisdiction: str | None = Query(default=None), principal: Principal = Depends(current_principal)) -> list:
    return DeadlineService(get_connection()).list_cases(principal, jurisdiction)


@router.get("/cases/{case_id}")
def case_detail(
    case_id: int,
    as_of: datetime | None = Query(default=None),
    principal: Principal = Depends(current_principal),
) -> dict:
    return DeadlineService(get_connection()).case_detail(principal, case_id, as_of)


@router.post("/cases/{case_id}/regenerate")
def regenerate_case(case_id: int, principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return DeadlineService(connection).regenerate_case(principal, case_id)


@router.post("/recompute")
def recompute(payload: RecomputeRequest | None = None, principal: Principal = Depends(current_principal)) -> dict:
    as_of = payload.as_of if payload else None
    with transaction(immediate=True) as connection:
        return DeadlineService(connection).recompute(principal, as_of)


@router.get("/nodes")
def list_nodes(
    as_of: datetime | None = Query(default=None),
    status: str | None = Query(default=None),
    display_status: str | None = Query(default=None),
    case_id: int | None = Query(default=None),
    jurisdiction: str | None = Query(default=None),
    due_before: date | None = Query(default=None),
    principal: Principal = Depends(current_principal),
) -> dict:
    return DeadlineService(get_connection()).list_nodes(
        principal,
        as_of=as_of,
        status=status,
        display_status=display_status,
        case_id=case_id,
        jurisdiction=jurisdiction,
        due_before=due_before,
    )


@router.post("/nodes/claim")
def claim_node(payload: ClaimRequest, principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        claimed = DeadlineService(connection).claim(
            principal, payload.worker_label, lease_seconds=payload.lease_seconds
        )
        return {"claimed": claimed}


@router.get("/nodes/{node_id}")
def node_detail(
    node_id: int,
    as_of: datetime | None = Query(default=None),
    principal: Principal = Depends(current_principal),
) -> dict:
    return DeadlineService(get_connection()).node_detail(principal, node_id, as_of)


@router.get("/nodes/{node_id}/explain")
def explain_node(
    node_id: int,
    as_of: datetime | None = Query(default=None),
    principal: Principal = Depends(current_principal),
) -> dict:
    return DeadlineService(get_connection()).explain(principal, node_id, as_of)


@router.post("/nodes/{node_id}/complete")
def complete_node(node_id: int, payload: CompleteRequest, principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return DeadlineService(connection).complete(principal, node_id, payload.worker_label, payload.resolution)


@router.post("/nodes/{node_id}/release")
def release_node(node_id: int, payload: ReleaseRequest, principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return DeadlineService(connection).release(principal, node_id, payload.worker_label)


@router.post("/nodes/{node_id}/extensions")
def extend_node(node_id: int, payload: ExtensionCreate, principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return DeadlineService(connection).record_extension(principal, node_id, payload.days, payload.reason)


@router.post("/nodes/{node_id}/payments")
def pay_node(node_id: int, payload: PaymentCreate, principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return DeadlineService(connection).record_payment(
            principal, node_id, payload.voucher_no, payload.paid_at, payload.note
        )
