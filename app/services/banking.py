"""Serve immutable training fixtures and validate durable banking writes."""

from __future__ import annotations

import asyncio
import csv
import json
from pathlib import Path
from typing import Any

from app.database.banking import MongoBankStore
from app.models.banking import BankError


class BankService:
    def __init__(self, store: MongoBankStore, root: Path) -> None:
        self.store = store
        self.customers = {
            c["customer_id"]: c for c in json.loads((root / "customers/customers.json").read_text())
        }
        with (root / "customers/transactions.csv").open() as stream:
            self.transactions = list(csv.DictReader(stream))

    def customer(self, customer_id: str, operation: str) -> dict[str, Any]:
        c = self.customers.get(customer_id)
        if c is None:
            raise BankError(404, "not-found", "Customer not found")
        if operation == "profile":
            result = {"profile": c["profile"]}
        elif operation == "accounts":
            result = {
                "accounts": [a for a in c["accounts"] if a["type"] != "loan"],
                "loans": [
                    {
                        **loan,
                        **(
                            {
                                "circ_2026_07_part_prepay_opt_in": c["flags"][
                                    "opted_in_cir_2026_07_part_prepay_minimum"
                                ]
                            }
                            if loan["product_id"] == "NEXA-HL"
                            else {}
                        ),
                    }
                    for loan in c["existing_loans"]
                ],
            }
        elif operation == "income":
            result = {
                "disclosed_income": c["disclosed_income"],
                "income_verification": {
                    k: c["income_verification"][k] for k in ("status", "method")
                },
            }
        elif operation == "bureau-summary":
            result = {k: v for k, v in c["bureau_summary"].items() if k != "note"}
        elif operation == "kyc-status":
            result = c["kyc"]
        elif operation == "applications":
            result = {"applications": c["open_applications"]}
        else:
            raise BankError(404, "not-found", "Unknown customer operation")
        return {"customer_id": customer_id, **result}

    def transaction_page(
        self, account: str, start: str | None, end: str | None, limit: int, cursor: str | None
    ) -> dict[str, Any]:
        if not any(
            a["account_id"] == account for c in self.customers.values() for a in c["accounts"]
        ):
            raise BankError(404, "not-found", "Account not found")
        rows = sorted(
            [
                r
                for r in self.transactions
                if r["account_id"] == account
                and (start is None or r["txn_date"] >= start)
                and (end is None or r["txn_date"] <= end)
            ],
            key=lambda r: (r["txn_date"], r["txn_id"]),
            reverse=True,
        )
        offset = int(cursor or "0")
        items = [
            {**r, "amount": int(r["amount"]), "balance_after": int(r["balance_after"])}
            for r in rows[offset : offset + limit]
        ]
        return {
            "items": items,
            "next_cursor": str(offset + limit) if offset + limit < len(rows) else None,
        }

    async def write(
        self, caller: str, path: str, key: str, body: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, str]]:
        if path in ("/v1/cases", "/v1/service-requests"):
            self.customer(body["customer_id"], "profile")
        if path == "/v1/service-requests" and body["request_type"] in {
            "INTEREST_CERTIFICATE",
            "OPT_IN_CIR_2026_07_PART_PREPAY",
        }:
            customer = self.customers[body["customer_id"]]
            loan = next(
                (
                    x
                    for x in customer["existing_loans"]
                    if x["loan_account_id"] == body["loan_account_id"]
                ),
                None,
            )
            if loan is None:
                raise BankError(
                    422, "validation-error", "Selected loan must belong to the customer"
                )
            if body["request_type"] == "OPT_IN_CIR_2026_07_PART_PREPAY":
                if customer["flags"]["opted_in_cir_2026_07_part_prepay_minimum"]:
                    raise BankError(409, "already-opted-in", "This customer has already opted in")
                if loan["version_id"] != "NHL-2025-10":
                    raise BankError(422, "not-applicable", "Loan is not eligible for this circular")
        return await asyncio.to_thread(self.store.execute, caller, path, key, body)
