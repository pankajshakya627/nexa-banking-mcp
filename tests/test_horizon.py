from __future__ import annotations

import asyncio
import json
import runpy
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import httpx2
import pytest
from cryptography.fernet import Fernet
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from mcp.shared.auth import OAuthToken
from pydantic import HttpUrl

from app.core.config import MCPSettings, get_mcp_settings
from app.mcp import horizon, horizon_login
from app.mcp.banking_http import signed_headers
from app.models.banking_mcp import BANKING_TOOLS, ServiceWrite, write_grant
from app.models.validation import ServiceRequestProposal, ServiceRequestReceipt, ValidationAccess
from app.services.banking_mcp import BankingMCPTransport, BankingOAuth, banking_oauth


def configuration(**kwargs):
    return MCPSettings(
        _env_file=None,
        banking_base_url="https://bank.example/",
        banking_api_token="backend" * 8,
        banking_mcp_signing_key="signature" * 8,
        banking_mcp_auth_mode="oauth",
        banking_mcp_transport="http",
        banking_mcp_url="https://mcp.example/mcp",
        **kwargs,
    )


def test_exported_server_requires_context_inside_tools(monkeypatch):
    config = configuration()
    monkeypatch.setattr(horizon, "get_mcp_settings", lambda: config)
    calls = []

    def respond(request):
        calls.append(request)
        if request.method == "POST":
            body = json.loads(request.content)
            return httpx.Response(
                201,
                json={
                    "service_request_id": "NXSR-000001",
                    "status": "RECEIVED",
                    "created_at": "2026-11-14T10:00:00+00:00",
                    **{k: body[k] for k in ("customer_id", "request_type", "loan_account_id")},
                },
            )
        return httpx.Response(200, json={"customer_id": "NXC-0010", "accounts": [], "loans": []})

    factory = httpx.AsyncClient
    monkeypatch.setattr(
        horizon.httpx,
        "AsyncClient",
        lambda **kw: factory(transport=httpx.MockTransport(respond), **kw),
    )

    async def run():
        app = horizon.mcp.http_app(path="/mcp", stateless_http=True, json_response=True)

        def transport(headers):
            return StreamableHttpTransport(
                "https://mcp.example/mcp",
                headers=headers,
                httpx_client_factory=lambda **kw: httpx2.AsyncClient(
                    transport=httpx2.ASGITransport(app=app), **kw
                ),
            )

        access = ValidationAccess(customer_ids=frozenset({"NXC-0010"}))
        headers = signed_headers(config, access, None)
        assert "Authorization" not in headers
        async with app.router.lifespan_context(app):
            async with Client(transport(headers)) as client:
                assert {t.name for t in await client.list_tools()} == BANKING_TOOLS
                assert (
                    await client.call_tool(
                        "list_accounts", {"request": {"customer_id": "NXC-0010"}}
                    )
                ).structured_content["status_code"] == 200
                with pytest.raises(ToolError):
                    await client.call_tool(
                        "list_accounts", {"request": {"customer_id": "NXC-0023"}}
                    )
                with pytest.raises(ToolError, match="grant"):
                    await client.call_tool(
                        "create_service_request",
                        {
                            "request": {
                                "payload": {"customer_id": "NXC-0010", "request_type": "CALLBACK"},
                                "idempotency_key": "horizon-test-key-0001",
                            }
                        },
                    )
            async with Client(transport({})) as client:
                with pytest.raises(ToolError, match="context"):
                    await client.call_tool(
                        "list_accounts", {"request": {"customer_id": "NXC-0010"}}
                    )
            proposal = ServiceWrite(
                payload=ServiceRequestProposal(customer_id="NXC-0010", request_type="CALLBACK"),
                idempotency_key="horizon-test-key-0002",
            ).model_dump(mode="json")
            granted_headers = signed_headers(
                config, access, write_grant("create_service_request", proposal)
            )
            async with Client(transport(granted_headers)) as client:
                receipt = await client.call_tool("create_service_request", {"request": proposal})
                assert receipt.structured_content["body"]["service_request_id"] == "NXSR-000001"
                with pytest.raises(ToolError, match="grant"):
                    await client.call_tool(
                        "create_service_request",
                        {"request": {**proposal, "idempotency_key": "changed-operation-key"}},
                    )
        assert len(calls) == 2
        assert calls[0].headers["Authorization"] == "Bearer " + "backend" * 8

    asyncio.run(run())


def test_horizon_startup_and_cli_preconditions(monkeypatch):
    monkeypatch.setattr(horizon, "get_mcp_settings", lambda: MCPSettings(_env_file=None))

    async def run():
        with pytest.raises(ValueError, match="HTTPS"):
            async with horizon.bank_lifespan(horizon.mcp):
                pytest.fail("Unsafe deployment must fail startup")
        with pytest.raises(ToolError):
            horizon.hosted_session()
        with pytest.raises(RuntimeError, match="Authorize Horizon"):
            await BankingOAuth().redirect_handler("https://example.invalid")
        monkeypatch.setattr(horizon_login, "get_mcp_settings", lambda: MCPSettings(_env_file=None))
        with pytest.raises(ValueError, match="HTTPS"):
            await horizon_login.login()

    asyncio.run(run())


def test_oauth_encrypted_cache_survives_new_client(tmp_path, monkeypatch):
    monkeypatch.setattr("fastmcp.client.auth.oauth.find_available_port", lambda **kw: 54321)
    config = configuration(
        banking_mcp_oauth_directory=tmp_path / "tokens",
        banking_mcp_oauth_storage_key=Fernet.generate_key().decode(),
    )

    async def run():
        first = banking_oauth(config, interactive=True)
        first._bind(str(config.banking_mcp_url))
        await first.token_storage_adapter.set_tokens(
            OAuthToken(
                access_token="synthetic-access-token",
                token_type="Bearer",
                refresh_token="synthetic-refresh-token",
                expires_in=3600,
            )
        )
        second = banking_oauth(config)
        second._bind(str(config.banking_mcp_url))
        tokens = await second.token_storage_adapter.get_tokens()
        assert tokens is not None and tokens.access_token == "synthetic-access-token"
        assert all(
            b"synthetic-access-token" not in file.read_bytes()
            for file in tmp_path.rglob("*")
            if file.is_file()
        )
        with pytest.raises(ValueError, match="encryption key"):
            banking_oauth(configuration())

    asyncio.run(run())


def test_login_checks_discovery_and_entrypoint(monkeypatch, tmp_path):
    config = configuration(
        banking_mcp_oauth_directory=tmp_path / "tokens",
        banking_mcp_oauth_storage_key=Fernet.generate_key().decode(),
    )
    monkeypatch.setattr(horizon_login, "get_mcp_settings", lambda: config)
    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    fake.list_tools.return_value = [SimpleNamespace(name=n) for n in BANKING_TOOLS]
    monkeypatch.setattr(horizon_login, "Client", lambda *a, **kw: fake)
    asyncio.run(horizon_login.login())
    fake.list_tools.return_value = []
    with pytest.raises(ValueError, match="Unexpected"):
        asyncio.run(horizon_login.login())
    entry = Mock(side_effect=lambda coro: coro.close())
    monkeypatch.setattr(asyncio, "run", entry)
    monkeypatch.delitem(sys.modules, "app.mcp.horizon_login")
    runpy.run_module("app.mcp.horizon_login", run_name="__main__")
    assert entry.call_count == 1


def test_remaining_config_validation_and_oauth_transport(monkeypatch):
    from app.api.dependencies import banking_client
    from app.core.telemetry import init_telemetry
    from app.models.banking import get_bank_settings

    monkeypatch.setenv("BANKING_MONGODB_URI", "mongodb://localhost")
    monkeypatch.setenv("BANKING_MONGODB_DB", "unit_bank")
    monkeypatch.setenv("BANKING_API_TOKEN", "backend" * 8)
    get_bank_settings.cache_clear()
    get_mcp_settings.cache_clear()
    assert get_bank_settings().banking_mongodb_db == "unit_bank"
    assert get_mcp_settings().banking_api_token is not None
    import ddtrace

    patched = Mock()
    monkeypatch.setattr(ddtrace, "patch_all", patched)
    init_telemetry(True)
    assert patched.call_count == 1
    with pytest.raises(ValueError):
        ServiceRequestProposal(customer_id="NXC-0010", request_type="INTEREST_CERTIFICATE")
    with pytest.raises(ValueError):
        ServiceRequestReceipt(
            customer_id="NXC-0010",
            request_type="CALLBACK",
            service_request_id=" ",
            status="RECEIVED",
            created_at="2026-01-01T00:00:00",
        )

    async def run():
        async for client in banking_client(MCPSettings(_env_file=None, banking_api_token=None)):
            assert client is None
        config = configuration()
        config.banking_mcp_url = HttpUrl("http://localhost/mcp")
        transport = BankingMCPTransport(
            config, ValidationAccess(customer_ids=frozenset({"NXC-0010"}))
        )
        with pytest.raises(httpx.ConnectError):
            await transport.handle_async_request(
                httpx.Request("GET", "https://bank.example/v1/customers/NXC-0010")
            )

    asyncio.run(run())
    get_bank_settings.cache_clear()
    get_mcp_settings.cache_clear()
