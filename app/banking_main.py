"""Render entry point for a persistent, authenticated synthetic banking API."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from pymongo.errors import PyMongoError
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from app.api.banking import router
from app.core.logging import configure_logging
from app.core.telemetry import init_telemetry
from app.database.banking import MongoBankStore
from app.models.banking import BankError, get_bank_settings
from app.services.banking import BankService


@asynccontextmanager
async def bank_lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_bank_settings()
    init_telemetry(settings.dd_trace_enabled)
    configure_logging(settings.log_level)
    store = MongoBankStore(settings)
    try:
        await asyncio.to_thread(store.initialize)
        app.state.bank = await asyncio.to_thread(BankService, store, settings.banking_data_root)
        yield
    finally:
        await asyncio.to_thread(store.close)


def create_banking_app() -> FastAPI:
    app = FastAPI(title="NexaBank Persistent Test API", version="1.0.0", lifespan=bank_lifespan)
    app.include_router(router)

    @app.exception_handler(BankError)
    async def bank_error(request: Request, exc: BankError) -> JSONResponse:
        return JSONResponse(
            {
                "type": f"https://api.nexabank.example/problems/{exc.kind}",
                "title": exc.title,
                "status": exc.status,
                "detail": None,
                "retryable": exc.status in {409, 503},
            },
            status_code=exc.status,
            media_type="application/problem+json",
            headers={"WWW-Authenticate": "Bearer"} if exc.status == 401 else {},
        )

    @app.exception_handler(PyMongoError)
    async def storage_error(request: Request, exc: PyMongoError) -> JSONResponse:
        structlog.get_logger().error("bank_storage_unavailable", error_type=type(exc).__name__)
        return JSONResponse(
            {"detail": "Bank storage unavailable; reconcile using the same operation key"},
            status_code=503,
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"detail": "Invalid banking request"}, status_code=422)

    @app.middleware("http")
    async def deny_fault_injection(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if "x-mock-fault" in request.headers:
            return JSONResponse({"detail": "Fault injection is disabled"}, status_code=400)
        return await call_next(request)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready(request: Request) -> dict[str, str]:
        await asyncio.to_thread(request.app.state.bank.store.ready)
        return {"status": "ready"}

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics(request: Request) -> str:
        await asyncio.to_thread(request.app.state.bank.store.ready)
        return "banking_storage_up 1\n"

    return app


app = create_banking_app()
