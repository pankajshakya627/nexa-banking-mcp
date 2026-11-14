# Project instructions

Read CONSTRAINTS.md before writing code. Do not weaken it to make a change pass.

Python 3.14+, uv and Pydantic v2. Keep API routers thin; business validation in
services, PyMongo access exclusively in database, MCP exposure in mcp, shared
contracts in models. Use typed async I/O or offload blocking database calls.
Preserve all twelve tool names and the /v1 banking payload contracts.

Horizon imports app/mcp/horizon.py:mcp. Security must execute on imported tool
calls, not only inside a CLI main function. Gateway OAuth and signed application
customer scope/exact write grants are separate requirements. Never weaken either
to make Inspector or a generic chat client work. No model may mint grants.

No real customer data or secrets. The bundled data is synthetic. Public reset and
fault injection exist only in the legacy test fixture; never mount it in deployment.
Commit bank mutations and idempotent replay results atomically in MongoDB Atlas.
Run make check-full and the explicit Atlas check before deployment. Finish pending
approved NexaAssist runs against their original backend before changing endpoints.
