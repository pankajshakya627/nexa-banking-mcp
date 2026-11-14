"""Persistent synthetic banking configuration and safe contract errors."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class BankSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    banking_mongodb_uri: SecretStr
    banking_mongodb_db: str = Field(min_length=1)
    banking_api_token: SecretStr = Field(min_length=32)
    banking_data_root: Path = Path(__file__).resolve().parents[1] / "data"
    dd_trace_enabled: bool = False
    log_level: str = "INFO"


@lru_cache
def get_bank_settings() -> BankSettings:
    return BankSettings()


class BankError(Exception):
    def __init__(self, status: int, kind: str, title: str) -> None:
        self.status, self.kind, self.title = status, kind, title
        super().__init__(title)
