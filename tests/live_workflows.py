"""Explicit live contract tests: Docker REST -> Atlas, called through a real HTTP MCP.

Uses trusted synthetic test grants, not a replacement for NexaAssist reviewer approval.
Run with BANKING_MONGODB_URI configured; only test-owned databases are removed.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from pymongo import MongoClient

from app.core.config import MCPSettings
from app.mcp.banking_http import signed_headers
from app.models.banking_mcp import (
    BANKING_TOOLS,
    CUSTOMER_TOOLS,
    CaseBody,
    CaseWrite,
    EscalationBody,
    EscalationWrite,
    NoteBody,
    NoteWrite,
    ServiceWrite,
    write_grant,
)
from app.models.validation import ServiceRequestProposal, ValidationAccess


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@pytest.fixture
def live_bank(tmp_path):
    uri = os.environ["BANKING_MONGODB_URI"]
    database = "bank_mcp_uat_" + uuid4().hex[:16]
    token, shared, signing = (secrets.token_urlsafe(48) for _ in range(3))
    api_port, mcp_port = free_port(), free_port()
    base = f"http://127.0.0.1:{api_port}/"
    config = MCPSettings(
        _env_file=None,
        banking_base_url=base,
        banking_api_token=token,
        banking_mcp_transport="http",
        banking_mcp_port=mcp_port,
        banking_mcp_url=f"http://127.0.0.1:{mcp_port}/mcp",
        banking_mcp_token=shared,
        banking_mcp_signing_key=signing,
    )
    credential_file = tmp_path / "backend.env"
    credential_file.write_text(
        f"BANKING_MONGODB_URI={uri}\nBANKING_MONGODB_DB={database}\nBANKING_API_TOKEN={token}\nPORT=18181\nDD_TRACE_ENABLED=false\n"
    )
    credential_file.chmod(0o600)
    container = None
    process = None
    mongo = MongoClient(uri, timeoutMS=5000, serverSelectionTimeoutMS=5000, tz_aware=True)
    api = httpx.Client(
        base_url=base, headers={"Authorization": "Bearer " + token}, timeout=10, trust_env=False
    )

    def start_backend():
        nonlocal container
        container = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-d",
                "-p",
                f"127.0.0.1:{api_port}:18181",
                "--env-file",
                str(credential_file),
                "nexa-banking-api:local",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout.strip()
        for _ in range(60):
            try:
                if api.get("ready").status_code == 200:
                    return
            except httpx.TransportError:
                time.sleep(0.25)
        raise AssertionError("Bank container did not become ready")

    def restart():
        subprocess.run(
            ["docker", "stop", "--time", "3", container],
            capture_output=True,
            check=True,
            timeout=15,
        )
        start_backend()

    try:
        start_backend()
        with (tmp_path / "mcp.log").open("w") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "app.mcp.banking_server"],
                stdout=log,
                stderr=log,
                env={
                    **os.environ,
                    "BANKING_BASE_URL": base,
                    "BANKING_API_TOKEN": token,
                    "BANKING_MCP_TRANSPORT": "http",
                    "BANKING_MCP_PORT": str(mcp_port),
                    "BANKING_MCP_TOKEN": shared,
                    "BANKING_MCP_SIGNING_KEY": signing,
                    "BANKING_MCP_CUSTOMER_IDS": "[]",
                    "BANKING_MCP_WRITE_GRANT": "",
                    "BANKING_MCP_AUTH_MODE": "token",
                    "DD_TRACE_ENABLED": "false",
                },
            )
            for _ in range(80):
                try:
                    if (
                        httpx.get(
                            str(config.banking_mcp_url), timeout=1, trust_env=False
                        ).status_code
                        == 401
                    ):
                        break
                except httpx.TransportError:
                    time.sleep(0.1)
            else:
                raise AssertionError("HTTP MCP did not become ready")
            yield SimpleNamespace(config=config, db=mongo[database], api=api, restart=restart)
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        api.close()
        if container:
            subprocess.run(
                ["docker", "rm", "-f", container], capture_output=True, check=False, timeout=15
            )
        try:
            mongo.drop_database(database)
        finally:
            mongo.close()
            credential_file.unlink(missing_ok=True)


def test_real_mcp_customer_case_and_service_journeys(live_bank):
    access = ValidationAccess(
        customer_ids=frozenset({"NXC-0010", "NXC-9999"}), principal_id="synthetic-owner"
    )
    observed = set()

    async def call(tool, arguments, *, grant=None, scope=access):
        transport = StreamableHttpTransport(
            str(live_bank.config.banking_mcp_url),
            headers=signed_headers(live_bank.config, scope, grant),
        )
        async with Client(transport) as client:
            assert {t.name for t in await client.list_tools()} == BANKING_TOOLS
            result = await client.call_tool(tool, {"request": arguments})
            observed.add(tool)
            assert result.structured_content is not None
            return result.structured_content

    async def write(tool, model):
        arguments = model.model_dump(mode="json")
        return await call(tool, arguments, grant=write_grant(tool, arguments))

    async def run():
        for tool in CUSTOMER_TOOLS:
            result = await call(tool, {"customer_id": "NXC-0010"})
            assert result["status_code"] == 200 and result["body"]["customer_id"] == "NXC-0010"
            with pytest.raises(ToolError):
                await call(tool, {"customer_id": "NXC-0023"})
        assert (await call("get_customer_profile", {"customer_id": "NXC-9999"}))[
            "status_code"
        ] == 404
        page = {"customer_id": "NXC-0010", "account_id": "NXA-100010", "limit": 2}
        first = await call("list_transactions", page)
        assert len(first["body"]["items"]) == 2
        second = await call("list_transactions", {**page, "cursor": first["body"]["next_cursor"]})
        assert first["body"]["items"] != second["body"]["items"]
        with pytest.raises(ToolError):
            await call("list_transactions", {**page, "account_id": "NXA-100001"})
        case_request = CaseWrite(
            payload=CaseBody(
                customer_id="NXC-0010",
                category="COMPLAINT",
                priority="HIGH",
                summary="Synthetic disputed fee case",
            ),
            idempotency_key=uuid4().hex,
        )
        with pytest.raises(ToolError):
            await call("create_case", case_request.model_dump(mode="json"))
        case = await write("create_case", case_request)
        assert case["status_code"] == 201
        cid = case["body"]["case_id"]
        assert (await call("get_case", {"customer_id": "NXC-0010", "case_id": cid}))[
            "body"
        ] == case["body"]
        replay = await write("create_case", case_request)
        assert replay["body"] == case["body"] and replay["headers"]["idempotent-replayed"] == "true"
        changed = case_request.model_copy(
            update={
                "payload": case_request.payload.model_copy(update={"summary": "Changed payload"})
            }
        )
        assert (await write("create_case", changed))["status_code"] == 422
        note = NoteWrite(
            customer_id="NXC-0010",
            case_id=cid,
            payload=NoteBody(text="Synthetic customer callback requested"),
            idempotency_key=uuid4().hex,
        )
        assert (await write("add_case_note", note))["status_code"] == 201
        assert (await write("add_case_note", note))["headers"]["idempotent-replayed"] == "true"
        escalation = EscalationWrite(
            customer_id="NXC-0010",
            case_id=cid,
            payload=EscalationBody(
                reason="COMPLAINT", summary="Synthetic escalation for disputed fee"
            ),
            idempotency_key=uuid4().hex,
        )
        assert (await write("escalate_case", escalation))["body"]["sla_hours"] == 24
        assert (
            await write(
                "escalate_case", escalation.model_copy(update={"idempotency_key": uuid4().hex})
            )
        )["status_code"] == 409
        snapshot = live_bank.db.bank_cases.find_one({"_id": cid})
        assert snapshot["_version"] == 3 and len(snapshot["notes"]) == 1
        foreign = ValidationAccess(
            customer_ids=frozenset({"NXC-0023"}), principal_id="synthetic-other"
        )
        with pytest.raises(ToolError):
            await call("get_case", {"customer_id": "NXC-0023", "case_id": cid}, scope=foreign)
        for kind in (
            "STATEMENT_COPY",
            "CALLBACK",
            "RE_KYC_REMINDER",
            "INTEREST_CERTIFICATE",
            "OPT_IN_CIR_2026_07_PART_PREPAY",
        ):
            request = ServiceWrite(
                payload=ServiceRequestProposal(
                    customer_id="NXC-0010",
                    request_type=kind,
                    loan_account_id="NXL-HL-0010"
                    if kind in {"INTEREST_CERTIFICATE", "OPT_IN_CIR_2026_07_PART_PREPAY"}
                    else None,
                ),
                idempotency_key=uuid4().hex,
            )
            receipt = await write("create_service_request", request)
            assert receipt["status_code"] == 201 and receipt["body"]["request_type"] == kind
            assert (await write("create_service_request", request))["body"] == receipt["body"]
        assert live_bank.db.bank_receipts.count_documents({}) == 5
        assert live_bank.db.bank_cases.count_documents({}) == 1
        assert (
            live_bank.api.get(
                "v1/customers/NXC-0010/accounts", headers={"Authorization": "Bearer incorrect"}
            ).status_code
            == 401
        )
        assert live_bank.api.post("_mock/reset").status_code == 404
        assert live_bank.api.get("health", headers={"X-Mock-Fault": "timeout"}).status_code == 400
        concurrent = ServiceWrite(
            payload=ServiceRequestProposal(customer_id="NXC-0010", request_type="CALLBACK"),
            idempotency_key=uuid4().hex,
        )
        receipts = await asyncio.gather(
            *(write("create_service_request", concurrent) for _ in range(4))
        )
        assert len({r["body"]["service_request_id"] for r in receipts}) == 1
        assert live_bank.db.bank_receipts.count_documents({}) == 6
        live_bank.restart()
        assert (await write("create_service_request", concurrent))["body"] == receipts[0]["body"]
        assert (await call("get_case", {"customer_id": "NXC-0010", "case_id": cid}))["body"][
            "status"
        ] == "ESCALATED"
        assert observed == BANKING_TOOLS
        headers = signed_headers(live_bank.config, access, None)
        context = json.loads(headers["X-Nexa-Banking-Context"])
        context["expires_at"] = int(time.time()) - 1
        payload = json.dumps(context)
        key = live_bank.config.banking_mcp_signing_key.get_secret_value()
        headers.update(
            {
                "X-Nexa-Banking-Context": payload,
                "X-Nexa-Banking-Signature": hmac.new(
                    key.encode(), payload.encode(), hashlib.sha256
                ).hexdigest(),
            }
        )
        assert (
            await asyncio.to_thread(
                httpx.post, str(live_bank.config.banking_mcp_url), headers=headers, timeout=5
            )
        ).status_code == 403
        headers["X-Nexa-Banking-Signature"] = "0" * 64
        assert (
            await asyncio.to_thread(
                httpx.post, str(live_bank.config.banking_mcp_url), headers=headers, timeout=5
            )
        ).status_code == 403
        assert live_bank.db.bank_receipts.count_documents({}) == 6

    asyncio.run(run())
