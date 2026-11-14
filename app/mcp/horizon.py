"""Importable Horizon server; Gateway OAuth never replaces application write grants."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.lifespan import lifespan

from app.core.config import get_mcp_settings
from app.core.logging import configure_logging
from app.core.telemetry import init_telemetry
from app.mcp.banking_http import verify_signed_context
from app.mcp.banking_server import build_banking_server
from app.models.validation import ValidationAccess


@lifespan
async def bank_lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    config = get_mcp_settings()
    if (
        config.banking_base_url.scheme != "https"
        or config.banking_api_token is None
        or config.banking_mcp_signing_key is None
    ):
        raise ValueError("Horizon requires an HTTPS backend, backend credential and signing key")
    configure_logging("INFO")
    init_telemetry(config.dd_trace_enabled)
    async with httpx.AsyncClient(
        base_url=str(config.banking_base_url).rstrip("/") + "/",
        headers={"Authorization": "Bearer " + config.banking_api_token.get_secret_value()},
        timeout=config.agent_http_timeout_seconds,
        follow_redirects=False,
    ) as client:
        yield {"bank_http": client}


def hosted_session() -> tuple[ValidationAccess, str | None]:
    try:
        headers = get_http_request().headers
        context = verify_signed_context(
            get_mcp_settings(),
            headers.get("x-nexa-banking-context", ""),
            headers.get("x-nexa-banking-signature", ""),
        )
    except ValueError, RuntimeError:
        raise ToolError("A valid application-issued banking context is required") from None
    return context.access, context.grant


mcp = build_banking_server(
    None,
    ValidationAccess(customer_ids=frozenset()),
    session_provider=hosted_session,
    lifespan=bank_lifespan,
)
