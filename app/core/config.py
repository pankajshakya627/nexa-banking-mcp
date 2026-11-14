"""Banking MCP settings; no dependency on NexaAssist, models or policy storage."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class MCPSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    agent_http_timeout_seconds: float = Field(default=60, gt=0, le=300)
    banking_transport: Literal["http", "mcp"] = "mcp"
    banking_mcp_transport: Literal["stdio", "http"] = "stdio"
    banking_mcp_auth_mode: Literal["token", "oauth"] = "token"
    banking_mcp_url: HttpUrl = HttpUrl("http://127.0.0.1:9002/mcp")
    banking_mcp_port: int = Field(default=9002, ge=1024, le=65535)
    banking_mcp_token: SecretStr | None = Field(default=None, min_length=32)
    banking_mcp_signing_key: SecretStr | None = Field(default=None, min_length=32)
    banking_mcp_customer_ids: frozenset[str] = Field(default_factory=frozenset)
    banking_mcp_write_grant: SecretStr | None = Field(default=None, exclude=True)
    banking_mcp_oauth_directory: Path = Path.home() / ".local/share/nexa-banking-mcp/oauth"
    banking_mcp_oauth_storage_key: SecretStr | None = None
    banking_base_url: HttpUrl = HttpUrl("http://127.0.0.1:8080/")
    banking_api_token: SecretStr | None = Field(default=None, min_length=1)
    dd_trace_enabled: bool = False

    @field_validator(
        "banking_api_token",
        "banking_mcp_write_grant",
        "banking_mcp_token",
        "banking_mcp_signing_key",
        "banking_mcp_oauth_storage_key",
        mode="before",
    )
    @classmethod
    def empty_secret(cls, value: object) -> object:
        return None if value == "" else value


@lru_cache
def get_mcp_settings() -> MCPSettings:
    return MCPSettings()
