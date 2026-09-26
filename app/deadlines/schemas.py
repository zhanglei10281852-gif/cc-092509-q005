from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field, field_validator

JURISDICTIONS = ("CN", "US", "EP")


class ApplicationCreate(BaseModel):
    application_code: str = Field(min_length=3, max_length=80)
    dossier_id: int | None = Field(default=None, gt=0)
    jurisdiction: str
    application_number: str = Field(min_length=2, max_length=80)
    title: str = Field(default="", max_length=200)
    filing_date: date
    priority_date: date | None = None
    publication_date: date | None = None
    grant_date: date | None = None
    lead_days: list[int] | None = Field(default=None)

    @field_validator("jurisdiction")
    @classmethod
    def _jurisdiction_supported(cls, value: str) -> str:
        if value not in JURISDICTIONS:
            raise ValueError(f"不支持的辖区，可选：{', '.join(JURISDICTIONS)}")
        return value

    @field_validator("lead_days")
    @classmethod
    def _lead_days_sane(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return value
        if not value or any(d < 0 or d > 3650 for d in value):
            raise ValueError("提前天数必须在 0 到 3650 之间")
        if len(set(value)) != len(value):
            raise ValueError("提前天数不能重复")
        return sorted(value, reverse=True)


class AnchorUpdate(BaseModel):
    priority_date: date | None = None
    publication_date: date | None = None
    grant_date: date | None = None
    title: str | None = Field(default=None, max_length=200)
    annuities_paid: int | None = Field(default=None, ge=0, le=30)


class ExtendRequest(BaseModel):
    new_due_date: date
    evidence_reference: str = Field(default="", max_length=200)
    note: str = Field(default="", max_length=500)


class PaymentRequest(BaseModel):
    evidence_reference: str = Field(min_length=3, max_length=200)
    note: str = Field(default="", max_length=500)
    paid_at: date | None = None


class ConfirmRequest(BaseModel):
    note: str = Field(default="", max_length=500)


class ReopenRequest(BaseModel):
    note: str = Field(default="", max_length=500)
