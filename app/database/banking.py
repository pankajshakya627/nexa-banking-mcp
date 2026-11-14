"""Commit bank mutations and their replay receipts in one Atlas transaction."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from pymongo import MongoClient, ReturnDocument, timeout
from pymongo.client_session import ClientSession
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern

from app.models.banking import BankError, BankSettings


class MongoBankStore:
    def __init__(self, settings: BankSettings) -> None:
        self.client: MongoClient[Any] = MongoClient(
            settings.banking_mongodb_uri.get_secret_value(),
            timeoutMS=5000,
            serverSelectionTimeoutMS=5000,
            tz_aware=True,
        )
        self.db: Database[Any] = self.client[settings.banking_mongodb_db]

    def initialize(self) -> None:
        with timeout(5):
            hello = self.client.admin.command("hello")
            if not (hello.get("setName") or hello.get("msg") == "isdbgrid"):
                raise ValueError("Bank storage requires transaction-capable MongoDB")
            for name in ("bank_cases", "bank_receipts", "bank_counters", "bank_idempotency"):
                if name not in self.db.list_collection_names():
                    self.db.create_collection(name)
            self.db.bank_idempotency.create_index("expires_at", expireAfterSeconds=0)

    def ready(self) -> None:
        with timeout(5):
            self.client.admin.command("ping")

    def close(self) -> None:
        self.client.close()

    def case(self, case_id: str, session: ClientSession | None = None) -> dict[str, Any]:
        row = self.db.bank_cases.find_one({"_id": case_id}, session=session)
        if row is None:
            raise BankError(404, "not-found", "Case not found")
        return row

    @staticmethod
    def case_view(row: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in row.items() if not k.startswith("_")}

    def _next(self, kind: str, session: ClientSession) -> str:
        row = self.db.bank_counters.find_one_and_update(
            {"_id": kind},
            {"$inc": {"value": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
            session=session,
        )
        if row is None or row["value"] > 999999:
            raise BankError(503, "capacity", "Synthetic identifier capacity reached")
        return f"{kind}-{row['value']:06d}"

    def execute(
        self, caller: str, path: str, key: str, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, str]]:
        identity = hashlib.sha256(json.dumps([caller, "POST", path, key]).encode()).hexdigest()
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

        def replay(record: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
            if record["digest"] != digest:
                raise BankError(
                    422, "idempotency-key-reuse", "Idempotency key used with another payload"
                )
            return record["body"], {**record["headers"], "Idempotent-Replayed": "true"}

        def commit(session: ClientSession) -> tuple[dict[str, Any], dict[str, str]]:
            now = datetime.now(UTC)
            previous = self.db.bank_idempotency.find_one({"_id": identity}, session=session)
            if previous is not None:
                if previous["expires_at"] > now:
                    return replay(previous)
                self.db.bank_idempotency.delete_one({"_id": identity}, session=session)
            self.db.bank_idempotency.insert_one(
                {"_id": identity, "digest": digest, "expires_at": now + timedelta(hours=24)},
                session=session,
            )
            body, headers = self._apply(path, payload, session)
            self.db.bank_idempotency.update_one(
                {"_id": identity}, {"$set": {"body": body, "headers": headers}}, session=session
            )
            return body, headers

        with timeout(5):
            try:
                with self.client.start_session() as session:
                    return session.with_transaction(
                        commit,
                        read_concern=ReadConcern("snapshot"),
                        write_concern=WriteConcern("majority", j=True),
                    )
            except DuplicateKeyError:
                existing = self.db.bank_idempotency.find_one({"_id": identity})
                if existing is not None and existing["expires_at"] > datetime.now(UTC):
                    return replay(existing)
                raise BankError(409, "request-in-progress", "Request is being processed") from None

    def _apply(
        self, path: str, payload: dict[str, Any], session: ClientSession
    ) -> tuple[dict[str, Any], dict[str, str]]:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        if path == "/v1/service-requests":
            identifier = self._next("NXSR", session)
            body = {
                "service_request_id": identifier,
                **{k: payload[k] for k in ("customer_id", "request_type", "loan_account_id")},
                "status": "RECEIVED",
                "created_at": now,
            }
            self.db.bank_receipts.insert_one({"_id": identifier, **body}, session=session)
            return body, {}
        if path == "/v1/cases":
            identifier = self._next("NXCASE", session)
            row = {
                "_id": identifier,
                "case_id": identifier,
                **{k: v for k, v in payload.items() if k != "channel"},
                "status": "OPEN",
                "created_at": now,
                "notes": [],
                "escalation": None,
                "_version": 1,
            }
            self.db.bank_cases.insert_one(row, session=session)
            return self.case_view(row), {"ETag": f'"{identifier}-1"'}
        identifier = path.split("/")[3]
        row = self.case(identifier, session)
        if path.endswith("/notes"):
            body = {
                "note_id": self._next("NXNOTE", session),
                "case_id": identifier,
                "text": payload["text"],
                "created_at": now,
            }
            row["notes"].append(body)
        else:
            if row["status"] == "ESCALATED":
                raise BankError(409, "already-escalated", "Case is already escalated")
            body = {
                "escalation_id": self._next("NXESC", session),
                "case_id": identifier,
                "status": "ESCALATED",
                "sla_hours": 4 if payload["reason"] == "SUSPECTED_FRAUD" else 24,
            }
            row.update(status="ESCALATED", escalation={**body, **payload})
        row["_version"] += 1
        self.db.bank_cases.replace_one({"_id": identifier}, row, session=session)
        return body, {}
