"""Nightly statement / ledger reconciliation.

Reads the day's bank statement export and the matching internal ledger
extract, compares the two, and writes a reconciliation report.
"""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass

# Ops keep asking for this to be tunable per entity. Hardcoded for now.
TOLERANCE_MINOR_UNITS = 0

MINOR_UNIT_DIGITS = 2

REPORT_COLUMNS = [
    "txn_id",
    "currency",
    "statement_amount",
    "ledger_amount",
    "difference_minor",
    "status",
]


@dataclass
class Entry:
    txn_id: str
    currency: str
    amount: float


def _load(path: str) -> dict[str, Entry]:
    entries: dict[str, Entry] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for record in csv.DictReader(fh):
            entry = Entry(
                txn_id=record["txn_id"],
                currency=record["currency"],
                amount=float(record["amount"]),
            )
            entries[entry.txn_id] = entry
    return entries


def _fmt(amount: float) -> str:
    return f"{amount:.{MINOR_UNIT_DIGITS}f}"


def reconcile(input_dir: str, out_dir: str) -> int:
    statement = _load(os.path.join(input_dir, "statement.csv"))
    ledger = _load(os.path.join(input_dir, "ledger.csv"))

    os.makedirs(out_dir, exist_ok=True)

    rows: list[list[str]] = []
    for txn_id, stmt in statement.items():
        led = ledger.get(txn_id)
        if led is None:
            rows.append([txn_id, stmt.currency, _fmt(stmt.amount), "", "", "STATEMENT_ONLY"])
            continue
        if led.currency != stmt.currency:
            rows.append(
                [
                    txn_id,
                    stmt.currency,
                    _fmt(stmt.amount),
                    _fmt(led.amount),
                    "",
                    "CURRENCY_MISMATCH",
                ]
            )
            continue
        difference_minor = round(abs(stmt.amount - led.amount) * 100)
        status = "MATCHED" if difference_minor <= TOLERANCE_MINOR_UNITS else "AMOUNT_MISMATCH"
        rows.append(
            [
                txn_id,
                stmt.currency,
                _fmt(stmt.amount),
                _fmt(led.amount),
                str(difference_minor),
                status,
            ]
        )

    report_path = os.path.join(out_dir, "reconciled.csv")
    with open(report_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(REPORT_COLUMNS)
        writer.writerows(rows)

    return 0
