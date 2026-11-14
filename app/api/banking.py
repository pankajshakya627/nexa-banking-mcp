"""Authenticated contract routes for the persistent synthetic bank."""

from __future__ import annotations

import asyncio
import hashlib
from secrets import compare_digest
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse

from app.models.banking import BankError, BankSettings, get_bank_settings
from app.models.banking_mcp import CaseBody, EscalationBody, NoteBody
from app.models.validation import ServiceRequestProposal
from app.services.banking import BankService

router = APIRouter(prefix="/v1")


def bank_service(request: Request) -> BankService:
    return request.app.state.bank


def bank_caller(
    settings: Annotated[BankSettings, Depends(get_bank_settings)],
    authorization: Annotated[str, Header()] = "",
) -> str:
    expected = "Bearer " + settings.banking_api_token.get_secret_value()
    if not compare_digest(authorization.encode(), expected.encode()):
        raise BankError(401, "unauthorized", "Banking credentials required")
    return hashlib.sha256(authorization.encode()).hexdigest()


Service = Annotated[BankService, Depends(bank_service)]
Caller = Annotated[str, Depends(bank_caller)]


def operation_key(key: Annotated[str, Header(alias="Idempotency-Key")] = "") -> str:
    if not 16 <= len(key) <= 64:
        raise BankError(
            400, "idempotency-key-required", "Idempotency-Key requires 16 to 64 characters"
        )
    return key


Key = Annotated[str, Depends(operation_key)]


@router.get("/customers/{customerId}")
async def profile(customerId: str, bank: Service, caller: Caller) -> dict[str, Any]:
    return bank.customer(customerId, "profile")


def register_customer_read(operation: str) -> None:
    async def read(customerId: str, bank: Service, caller: Caller) -> dict[str, Any]:
        return bank.customer(customerId, operation)

    router.add_api_route(
        "/customers/{customerId}/" + operation, read, methods=["GET"], name=operation
    )


for operation in ("accounts", "income", "bureau-summary", "kyc-status", "applications"):
    register_customer_read(operation)


@router.get("/accounts/{accountId}/transactions")
async def transactions(
    accountId: str,
    bank: Service,
    caller: Caller,
    start: Annotated[str | None, Query(alias="from")] = None,
    to: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query(pattern=r"^[0-9]{1,20}$")] = None,
) -> dict[str, Any]:
    return bank.transaction_page(accountId, start, to, limit, cursor)


@router.get("/cases/{caseId}")
async def case(caseId: str, bank: Service, caller: Caller) -> JSONResponse:
    row = await asyncio.to_thread(bank.store.case, caseId)
    return JSONResponse(
        bank.store.case_view(row), headers={"ETag": f'"{caseId}-{row["_version"]}"'}
    )


@router.post("/cases", status_code=201)
async def create_case(body: CaseBody, key: Key, bank: Service, caller: Caller) -> JSONResponse:
    result, headers = await bank.write(caller, "/v1/cases", key, body.model_dump(mode="json"))
    return JSONResponse(result, status_code=201, headers=headers)


@router.post("/cases/{caseId}/notes", status_code=201)
async def add_note(
    caseId: str, body: NoteBody, key: Key, bank: Service, caller: Caller
) -> JSONResponse:
    result, headers = await bank.write(
        caller, f"/v1/cases/{caseId}/notes", key, body.model_dump(mode="json")
    )
    return JSONResponse(result, status_code=201, headers=headers)


@router.post("/cases/{caseId}/escalations", status_code=201)
async def escalate(
    caseId: str, body: EscalationBody, key: Key, bank: Service, caller: Caller
) -> JSONResponse:
    result, headers = await bank.write(
        caller, f"/v1/cases/{caseId}/escalations", key, body.model_dump(mode="json")
    )
    return JSONResponse(result, status_code=201, headers=headers)


@router.post("/service-requests", status_code=201)
async def create_request(
    body: ServiceRequestProposal, key: Key, bank: Service, caller: Caller
) -> JSONResponse:
    result, headers = await bank.write(
        caller, "/v1/service-requests", key, body.model_dump(mode="json")
    )
    return JSONResponse(result, status_code=201, headers=headers)
