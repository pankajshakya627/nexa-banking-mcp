"""Explicit integration run: pytest tests/live_atlas.py. Requires BANKING_MONGODB_URI.

Creates and drops only a fresh banking_smoke_<uuid> database, never the configured bank DB.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest

from app.database.banking import MongoBankStore
from app.models.banking import BankError, BankSettings
from tests.test_persistent_bank import CASE, ESCALATION, KEY, NOTE, TOKEN


def test_atlas_restart_concurrency_and_uncertain_commit():
    settings = BankSettings(
        banking_mongodb_db="banking_smoke_" + uuid4().hex[:20], banking_api_token=TOKEN
    )
    first = MongoBankStore(settings)
    try:
        first.initialize()
        first.ready()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(
                pool.map(
                    lambda _: first.execute("synthetic-caller", "/v1/cases", KEY, CASE), range(4)
                )
            )
        assert len({r[0]["case_id"] for r in results}) == 1
        assert first.db.bank_cases.count_documents({}) == 1
        assert first.db.bank_idempotency.count_documents({}) == 1
        case_id = results[0][0]["case_id"]
        first.close()
        restarted = MongoBankStore(settings)
        try:
            restarted.initialize()
            replay = restarted.execute("synthetic-caller", "/v1/cases", KEY, CASE)
            assert replay[0] == results[0][0] and replay[1]["Idempotent-Replayed"] == "true"
            assert restarted.case(case_id)["case_id"] == case_id
            with pytest.raises(BankError) as changed:
                restarted.execute(
                    "synthetic-caller",
                    "/v1/cases",
                    KEY,
                    {**CASE, "summary": "Changed synthetic payload"},
                )
            assert changed.value.status == 422

            note_path = f"/v1/cases/{case_id}/notes"

            # The reply can be lost after commit; only the original key reconciles it.
            def lost_reply():
                restarted.execute("synthetic-caller", note_path, KEY, NOTE)
                raise httpx.ReadTimeout("Synthetic reply lost after commit")

            with pytest.raises(httpx.ReadTimeout):
                lost_reply()
            note = restarted.execute("synthetic-caller", note_path, KEY, NOTE)
            assert note[1]["Idempotent-Replayed"] == "true"
            assert len(restarted.case(case_id)["notes"]) == 1
            escalation = restarted.execute(
                "synthetic-caller", f"/v1/cases/{case_id}/escalations", KEY, ESCALATION
            )
            assert escalation[0]["sla_hours"] == 4
            assert restarted.case(case_id)["escalation"]["preferred_contact"] == "EMAIL"
            before = restarted.db.bank_idempotency.count_documents({})
            with pytest.raises(BankError):
                restarted.execute("synthetic-caller", "/v1/cases/NXCASE-999999/notes", KEY, NOTE)
            assert restarted.db.bank_idempotency.count_documents({}) == before
            for kind in (
                "CALLBACK",
                "STATEMENT_COPY",
                "RE_KYC_REMINDER",
                "INTEREST_CERTIFICATE",
                "OPT_IN_CIR_2026_07_PART_PREPAY",
            ):
                payload = {
                    "customer_id": "NXC-0010",
                    "request_type": kind,
                    "loan_account_id": "NXL-HL-0010"
                    if kind in {"INTEREST_CERTIFICATE", "OPT_IN_CIR_2026_07_PART_PREPAY"}
                    else None,
                }
                result = restarted.execute(
                    "synthetic-caller", "/v1/service-requests", KEY + kind[:8], payload
                )
                again = restarted.execute(
                    "synthetic-caller", "/v1/service-requests", KEY + kind[:8], payload
                )
                assert result[0] == again[0] and again[1]["Idempotent-Replayed"] == "true"
            assert restarted.db.bank_receipts.count_documents({}) == 5
        finally:
            restarted.client.drop_database(settings.banking_mongodb_db)
            restarted.close()
    finally:
        first.close()
