from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field


class CaseCreate(BaseModel):
    case_code: str = Field(min_length=3, max_length=60)
    title: str = Field(min_length=2, max_length=200)
    jurisdiction: str = Field(min_length=2, max_length=10)
    application_number: str = Field(min_length=3, max_length=80)
    application_date: date
    priority_date: date | None = None
    publication_date: date | None = None
    grant_date: date | None = None
    owner_user_id: int | None = Field(default=None, gt=0)


class RecomputeRequest(BaseModel):
    as_of: datetime | None = None


class ClaimRequest(BaseModel):
    worker_label: str = Field(min_length=2, max_length=80)
    lease_seconds: int = Field(default=3600, ge=60, le=86400)


class CompleteRequest(BaseModel):
    worker_label: str = Field(min_length=2, max_length=80)
    resolution: str = Field(min_length=2, max_length=500)


class ReleaseRequest(BaseModel):
    worker_label: str = Field(min_length=2, max_length=80)


class ExtensionCreate(BaseModel):
    days: int = Field(gt=0, le=3660)
    reason: str = Field(min_length=2, max_length=500)


class PaymentCreate(BaseModel):
    voucher_no: str = Field(min_length=3, max_length=80)
    paid_at: date
    note: str = Field(default="", max_length=500)
