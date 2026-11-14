"""Scoped HTTPX client for integrations; the server does not require this adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx

from app.core.config import MCPSettings
from app.models.validation import ValidationAccess
from app.services.banking_mcp import BankingMCPTransport


async def banking_client(
    settings: MCPSettings, access: ValidationAccess | None = None
) -> AsyncIterator[httpx.AsyncClient | None]:
    if settings.banking_api_token is None:
        yield None
        return
    async with httpx.AsyncClient(
        base_url=str(settings.banking_base_url).rstrip("/") + "/",
        transport=BankingMCPTransport(settings, access)
        if settings.banking_transport == "mcp"
        else None,
        headers={"Authorization": "Bearer " + settings.banking_api_token.get_secret_value()},
        timeout=settings.agent_http_timeout_seconds,
        follow_redirects=False,
    ) as client:
        yield client
