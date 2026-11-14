"""Typed banking MCP arguments and a status-preserving result envelope."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.validation import ServiceRequestProposal

type CustomerId = Annotated[str, Field(pattern=r"^NXC-[0-9]{4}$")]
type RecordId = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")]
type OperationKey = Annotated[str, Field(min_length=16, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]

CUSTOMER_TOOLS = {
    "get_customer_profile": "",
    "list_accounts": "/accounts",
    "get_kyc_status": "/kyc-status",
    "get_bureau_summary": "/bureau-summary",
    "get_income": "/income",
    "list_applications": "/applications",
}
BANKING_TOOLS = frozenset(CUSTOMER_TOOLS) | {
    "list_transactions",
    "get_case",
    "create_case",
    "add_case_note",
    "escalate_case",
    "create_service_request",
}


class CustomerRead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_id: CustomerId


class TransactionRead(CustomerRead):
    account_id: RecordId
    from_date: date | None = None
    to_date: date | None = None
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = Field(default=None, pattern=r"^[0-9]{1,20}$")

    @model_validator(mode="after")
    def ordered_dates(self) -> TransactionRead:
        if self.from_date and self.to_date and self.from_date > self.to_date:
            raise ValueError("Transaction start date must not follow end date")
        return self


class CaseRead(CustomerRead):
    case_id: Annotated[str, Field(pattern=r"^NXCASE-[0-9]{6}$")]


class CaseBody(CustomerRead):
    category: Literal[
        "LOAN_ENQUIRY",
        "KYC_ISSUE",
        "FEE_WAIVER_REQUEST",
        "COMPLAINT",
        "SUSPECTED_FRAUD",
        "DOCUMENT_ANOMALY",
        "OTHER",
    ]
    priority: Literal["LOW", "NORMAL", "HIGH"]
    summary: str = Field(min_length=5, max_length=1000)
    related_application_id: str | None = None
    channel: Literal["ASSISTANT"] = "ASSISTANT"


class NoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=2000)


class EscalationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Literal[
        "COMPLAINT",
        "FEE_WAIVER_OR_SPECIAL_RATE",
        "SUSPECTED_FRAUD",
        "HARDSHIP",
        "KYC_DISCREPANCY",
        "UPLOAD_WITH_EMBEDDED_INSTRUCTIONS",
        "OUT_OF_SCOPE_REQUEST",
    ]
    summary: str = Field(min_length=5, max_length=1000)
    preferred_contact: Literal["CALLBACK", "BRANCH_VISIT", "EMAIL"] = "CALLBACK"


class CaseWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    payload: CaseBody
    idempotency_key: OperationKey


class NoteWrite(CaseRead):
    payload: NoteBody
    idempotency_key: OperationKey


class EscalationWrite(CaseRead):
    payload: EscalationBody
    idempotency_key: OperationKey


class ServiceWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    payload: ServiceRequestProposal
    idempotency_key: OperationKey


class BankingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status_code: int = Field(ge=200, le=599)
    body: dict[str, Any]
    headers: dict[str, str] = Field(default_factory=dict)


def write_grant(tool: str, arguments: dict[str, Any]) -> str:
    """Bind an internal one-operation capability to the complete canonical arguments."""
    canonical = json.dumps(
        [tool, arguments], sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(canonical.encode()).hexdigest()
