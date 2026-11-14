"""Standalone banking contracts shared by MCP and the persistent test API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

type ServiceRequestType = Literal[
    "STATEMENT_COPY",
    "CALLBACK",
    "RE_KYC_REMINDER",
    "OPT_IN_CIR_2026_07_PART_PREPAY",
    "INTEREST_CERTIFICATE",
]


class ServiceRequestDetails(BaseModel):
    """Service-request type, selected loan and optional notes.

    Attributes:
        request_type: Supported service-request category selected for the operation.
        loan_account_id: Existing loan account selected for ownership and eligibility checks.
        notes: Optional bounded service-request notes, screened before model/tool execution.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    request_type: ServiceRequestType
    loan_account_id: str | None = Field(default=None, min_length=1, max_length=100)
    notes: str | None = Field(default=None, max_length=500)


class ServiceRequestProposal(ServiceRequestDetails):
    """Service-request details tied to an authorized customer.

    Attributes:
        customer_id: Synthetic customer identifier within the authorized scope.
    """

    customer_id: str = Field(pattern=r"^NXC-[0-9]{4}$")

    @model_validator(mode="after")
    def required_loan_is_present(self) -> ServiceRequestProposal:
        """Require an owned loan selection for certificate and opt-in proposals.

        Raises:
            ValueError: This service-request proposal requires a selected loan.
        """
        if (
            self.request_type in ("OPT_IN_CIR_2026_07_PART_PREPAY", "INTEREST_CERTIFICATE")
            and self.loan_account_id is None
        ):
            raise ValueError("This service-request proposal requires a selected loan")
        return self


class ValidationAccess(BaseModel):
    """Server-issued scope, never populated from model or request-body claims.

    Attributes:
        customer_ids: Customer identifiers this configured principal may access.
        principal_id: Unique configured staff identity; independent of customer identifiers.
        can_approve: Whether this configured principal may decide another owner's proposal.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    customer_ids: frozenset[str]
    principal_id: str = "training"
    can_approve: bool = False


class CustomerAccounts(BaseModel):
    """Customer identity, deposit accounts and loans returned by the bank.

    Attributes:
        customer_id: Synthetic customer identifier within the authorized scope.
        accounts: Current non-loan account records returned by the banking integration.
        loans: Current customer loan records supplied by the banking integration.
    """

    model_config = ConfigDict(extra="forbid")

    customer_id: str = Field(pattern=r"^NXC-[0-9]{4}$")
    accounts: list[dict[str, Any]]
    loans: list[dict[str, Any]]


class ServiceRequestReceipt(BaseModel):
    """Bank receipt for a received service request.

    Attributes:
        customer_id: Synthetic customer identifier within the authorized scope.
        request_type: Supported service-request category selected for the operation.
        loan_account_id: Existing loan account selected for ownership and eligibility checks.
        status: Receipt or submission state.
        created_at: Timezone-aware creation timestamp of the saved run or receipt.
    """

    model_config = ConfigDict(extra="forbid")

    service_request_id: str = Field(min_length=1)
    customer_id: str = Field(pattern=r"^NXC-[0-9]{4}$")
    request_type: ServiceRequestType
    loan_account_id: str | None = None
    status: Literal["RECEIVED"]
    created_at: datetime

    @model_validator(mode="after")
    def valid_receipt_identity_and_time(self) -> ServiceRequestReceipt:
        """Reject blank receipt identities and timestamps without a timezone.

        Raises:
            ValueError: Banking receipt requires a nonblank ID and timezone-aware timestamp.
        """
        if not self.service_request_id.strip() or self.created_at.utcoffset() is None:
            raise ValueError("Banking receipt requires a nonblank ID and timezone-aware timestamp")
        return self
