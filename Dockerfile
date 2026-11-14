FROM python:3.14.8-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv
WORKDIR /service
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,id=nexa-banking-uv,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
COPY app ./app
RUN useradd --create-home --uid 10001 bank && chown -R bank:bank /service
USER bank
ENV PATH="/service/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["sh", "-c", "exec ddtrace-run uvicorn app.banking_main:app --host 0.0.0.0 --port ${PORT:-8080}"]
