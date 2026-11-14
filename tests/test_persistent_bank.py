from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, Mock

import mongomock
import pytest
import yaml
from fastapi.testclient import TestClient
from pymongo.errors import DuplicateKeyError, PyMongoError

from app.banking_main import create_banking_app
from app.database.banking import MongoBankStore
from app.models.banking import BankError, BankSettings, get_bank_settings
from app.models.banking_mcp import CaseBody
from app.services.banking import BankService

TOKEN = "synthetic-backend-credential-00000001"
ROOT = Path(__file__).resolve().parents[1] / "app/data"
CASE = CaseBody(
    customer_id="NXC-0010", category="OTHER", priority="NORMAL", summary="Synthetic test case"
).model_dump(mode="json")
NOTE = {"text": "Synthetic note"}
ESCALATION = {
    "reason": "SUSPECTED_FRAUD",
    "summary": "Synthetic escalation",
    "preferred_contact": "EMAIL",
}
KEY = "synthetic-idempotency-0001"


class SessionlessCollection:
    """Unit double only; real transaction guarantees are checked by atlas_smoke."""

    def __init__(self, collection):
        self.collection = collection

    def __getattr__(self, name):
        def call(*args, **kwargs):
            kwargs.pop("session", None)
            return getattr(self.collection, name)(*args, **kwargs)

        return call


class SessionlessDB:
    def __init__(self):
        self.raw = mongomock.MongoClient(tz_aware=True).bank

    def __getattr__(self, name):
        return SessionlessCollection(self.raw[name])


@pytest.fixture
def settings():
    return BankSettings(
        _env_file=None,
        banking_mongodb_uri="mongodb://localhost",
        banking_mongodb_db="unit_bank",
        banking_api_token=TOKEN,
    )


@pytest.fixture
def store(settings):
    store = MongoBankStore(settings)
    store.close()
    store.db = SessionlessDB()
    store.client = MagicMock()
    session = store.client.start_session.return_value.__enter__ = Mock()
    context = Mock()
    context.with_transaction.side_effect = lambda callback, **kwargs: callback(None)
    session.return_value = context
    store.client.start_session.return_value.__exit__ = Mock(return_value=False)
    return store


@pytest.fixture
def client(settings, store, monkeypatch):
    monkeypatch.setattr("app.banking_main.MongoBankStore", lambda _: store)
    monkeypatch.setattr(store, "initialize", Mock())
    app = create_banking_app()
    app.dependency_overrides[get_bank_settings] = lambda: settings
    monkeypatch.setattr("app.banking_main.get_bank_settings", lambda: settings)
    with TestClient(app, headers={"Authorization": "Bearer " + TOKEN}) as client:
        yield client


def test_read_contract_and_secret_boundaries(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").status_code == 200
    assert client.get("/metrics").text == "banking_storage_up 1\n"
    spec = yaml.safe_load((ROOT / "openapi.yaml").read_text())
    actual = client.get("/openapi.json").json()
    assert set(spec["paths"]) <= set(actual["paths"])
    for path in ("", "/accounts", "/income", "/bureau-summary", "/kyc-status", "/applications"):
        result = client.get("/v1/customers/NXC-0010" + path)
        assert result.status_code == 200 and result.json()["customer_id"] == "NXC-0010"
    assert "note" not in client.get("/v1/customers/NXC-0010/bureau-summary").json()
    assert client.get("/v1/customers/NXC-9999").status_code == 404
    assert client.get("/v1/accounts/unknown/transactions").status_code == 404
    first = client.get("/v1/accounts/NXA-100001/transactions?limit=1").json()
    assert len(first["items"]) == 1 and first["next_cursor"] == "1"
    second = client.get("/v1/accounts/NXA-100001/transactions?limit=1&cursor=1").json()
    assert first["items"] != second["items"]
    assert client.get(
        "/v1/accounts/NXA-100001/transactions?from=2100-01-01&to=2100-01-02"
    ).json() == {"items": [], "next_cursor": None}
    assert (
        client.get("/v1/customers/NXC-0010", headers={"Authorization": "Bearer wrong"}).status_code
        == 401
    )
    assert client.post("/_mock/reset").status_code == 404
    assert client.get("/health", headers={"X-Mock-Fault": "timeout"}).status_code == 400
    assert client.post("/v1/cases", json=CASE).status_code == 400
    assert (
        client.post(
            "/v1/cases", json={**CASE, "unexpected": True}, headers={"Idempotency-Key": KEY}
        ).status_code
        == 422
    )


def test_all_case_writes_and_replay(client, store):
    headers = {"Idempotency-Key": KEY}
    response = client.post("/v1/cases", json=CASE, headers=headers)
    assert response.status_code == 201
    case = response.json()
    path = "/v1/cases/" + case["case_id"]
    assert response.headers["etag"] == '"NXCASE-000001-1"'
    assert client.get(path).json() == case
    replay = client.post("/v1/cases", json=CASE, headers=headers)
    assert replay.json() == case and replay.headers["Idempotent-Replayed"] == "true"
    assert (
        client.post(
            "/v1/cases", json={**CASE, "summary": "Changed synthetic case"}, headers=headers
        ).status_code
        == 422
    )
    assert (
        client.post(path + "/notes", json=NOTE, headers=headers).json()["note_id"]
        == "NXNOTE-000001"
    )
    escalation = client.post(path + "/escalations", json=ESCALATION, headers=headers)
    assert escalation.status_code == 201 and escalation.json()["sla_hours"] == 4
    assert "preferred_contact" not in escalation.json()
    latest = client.get(path)
    assert latest.headers["etag"] == '"NXCASE-000001-3"'
    assert latest.json()["escalation"]["preferred_contact"] == "EMAIL"
    assert (
        client.post(
            path + "/escalations", json=ESCALATION, headers={"Idempotency-Key": KEY + "2"}
        ).status_code
        == 409
    )
    assert client.get("/v1/cases/NXCASE-999999").status_code == 404
    assert store.db.raw.bank_cases.count_documents({}) == 1


@pytest.mark.parametrize(
    "kind",
    [
        "CALLBACK",
        "STATEMENT_COPY",
        "RE_KYC_REMINDER",
        "INTEREST_CERTIFICATE",
        "OPT_IN_CIR_2026_07_PART_PREPAY",
    ],
)
def test_service_types_keep_receipts(client, kind):
    payload = {
        "customer_id": "NXC-0010",
        "request_type": kind,
        "loan_account_id": "NXL-HL-0010"
        if kind in {"INTEREST_CERTIFICATE", "OPT_IN_CIR_2026_07_PART_PREPAY"}
        else None,
    }
    result = client.post("/v1/service-requests", json=payload, headers={"Idempotency-Key": KEY})
    assert result.status_code == 201
    assert (
        result.json()["status"] == "RECEIVED"
        and result.json()["loan_account_id"] == payload["loan_account_id"]
    )
    assert (
        client.post("/v1/service-requests", json=payload, headers={"Idempotency-Key": KEY}).json()
        == result.json()
    )


def test_store_replay_lifetime_and_failure_modes(store):
    first = store.execute("caller", "/v1/cases", KEY, CASE)
    store.db.raw.bank_idempotency.update_many(
        {}, {"$set": {"expires_at": datetime.now(UTC) - timedelta(seconds=1)}}
    )
    assert store.execute("caller", "/v1/cases", KEY, CASE)[0] != first[0]
    assert store.execute("other-caller", "/v1/cases", KEY, CASE)[0] != first[0]
    store.db.raw.bank_counters.update_one({"_id": "NXCASE"}, {"$set": {"value": 999999}})
    with pytest.raises(BankError, match="capacity"):
        store.execute("new-caller", "/v1/cases", KEY, CASE)
    store.db.raw.bank_counters.delete_many({})
    existing = store.db.raw.bank_idempotency.find_one({"body": {"$exists": True}})
    assert existing is not None
    store.client.start_session.return_value.__enter__.return_value.with_transaction.side_effect = (
        DuplicateKeyError("race")
    )
    # A duplicate claim can only replay a committed matching operation.
    with pytest.raises(BankError, match="processed"):
        store.execute("not-committed", "/v1/cases", KEY, CASE)


def test_validation_and_storage_outage(client, store):
    payload = {
        "customer_id": "NXC-0010",
        "request_type": "INTEREST_CERTIFICATE",
        "loan_account_id": "not-owned",
    }
    assert (
        client.post(
            "/v1/service-requests", json=payload, headers={"Idempotency-Key": KEY}
        ).status_code
        == 422
    )
    store.client.admin.command.side_effect = PyMongoError("secret database URI")
    response = client.get("/ready")
    assert response.status_code == 503 and "secret" not in response.text
    service = BankService(store, ROOT)
    with pytest.raises(BankError):
        service.customer("NXC-0010", "unknown")
    with pytest.raises(BankError):
        asyncio.run(service.write("caller", "/v1/cases", KEY, {**CASE, "customer_id": "NXC-9999"}))


def test_initialization_requires_transactions(settings, monkeypatch):
    client = MagicMock()
    client.admin.command.return_value = {}
    monkeypatch.setattr("app.database.banking.MongoClient", lambda *a, **kw: client)
    store = MongoBankStore(settings)
    with pytest.raises(ValueError, match="transaction-capable"):
        store.initialize()
    client.admin.command.return_value = {"setName": "replica"}
    client.__getitem__ = Mock(return_value=Mock())
    store.db = client[settings.banking_mongodb_db]
    store.db.list_collection_names.return_value = []
    store.initialize()
    assert store.db.create_collection.call_count == 4
    store.ready()
    store.close()


def test_concurrent_duplicate_replays_committed_result(store):
    first = store.execute("caller", "/v1/cases", KEY, CASE)
    store.client.start_session.return_value.__enter__.return_value.with_transaction.side_effect = (
        DuplicateKeyError("race")
    )
    replay = store.execute("caller", "/v1/cases", KEY, CASE)
    assert replay[0] == first[0] and replay[1]["Idempotent-Replayed"] == "true"


@pytest.mark.parametrize(
    "customer,loan,status", [("NXC-0023", "NXL-HL-0023", 409), ("NXC-0001", "NXL-HL-0001", 422)]
)
def test_opt_in_requires_eligible_unenrolled_loan(client, customer, loan, status):
    response = client.post(
        "/v1/service-requests",
        json={
            "customer_id": customer,
            "loan_account_id": loan,
            "request_type": "OPT_IN_CIR_2026_07_PART_PREPAY",
        },
        headers={"Idempotency-Key": KEY},
    )
    assert response.status_code == status
