#!/bin/bash
# Reference solution: replace the reconciliation core with a correct implementation.
set -euo pipefail

cat > /app/reconciler/core.py << 'PYEOF'
"""Nightly statement / ledger reconciliation.

Reads the day's bank statement export and the matching internal ledger
extract, nets settlement groups, compares the two sides, and writes a
reconciliation report plus a quarantine file listing unusable rows.
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
import tomllib
from collections import defaultdict
from decimal import Decimal
from typing import NamedTuple

REPORT_COLUMNS = [
    "txn_id",
    "currency",
    "statement_amount",
    "ledger_amount",
    "ledger_entry_count",
    "difference_minor",
    "status",
]
QUARANTINE_COLUMNS = ["source_file", "line_number", "reason"]

REQUIRED_FILES = ("statement.csv", "ledger.csv", "config.toml", "currencies.json")
AMOUNT_RE = re.compile(r"^-?\d+(\.\d+)?$")

STATEMENT_FIELDS = 4
LEDGER_FIELDS = 4


class Entry(NamedTuple):
    txn_id: str
    settlement_ref: str
    currency: str
    amount: Decimal


class LedgerSide(NamedTuple):
    """The ledger half of one comparison: a direct entry or a netted group."""

    currency: str | None  # None when members disagree on currency
    total: Decimal | None  # None when currency is None
    count: int


def _format_amount(amount: Decimal, digits: int) -> str:
    return f"{amount:.{digits}f}"


def _fractional_digits(text: str) -> int:
    if "." in text:
        return len(text.split(".", 1)[1])
    return 0


def _load_tolerance(path: str) -> int:
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    tolerance = data["reconciliation"]["tolerance_minor_units"]
    if isinstance(tolerance, bool) or not isinstance(tolerance, int):
        raise ValueError("tolerance_minor_units must be an integer")
    return tolerance


def _load_currencies(path: str) -> dict[str, int]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("currencies.json must contain an object")
    for code, digits in data.items():
        if isinstance(digits, bool) or not isinstance(digits, int) or digits < 0:
            raise ValueError(f"bad minor unit digits for {code}")
    return data


def _read_rows(
    path: str,
    field_count: int,
    currency_index: int,
    amount_index: int,
    currencies: dict[str, int],
    quarantine: list[tuple[str, int, str]],
) -> list[list[str]]:
    """Return the valid rows of a file, appending failures to quarantine."""
    source_file = os.path.basename(path)
    valid: list[list[str]] = []

    with open(path, newline="", encoding="utf-8") as fh:
        for line_number, row in enumerate(csv.reader(fh), start=1):
            if line_number == 1:
                continue
            if len(row) != field_count:
                quarantine.append((source_file, line_number, "MALFORMED_ROW"))
                continue
            if not row[0].strip():
                quarantine.append((source_file, line_number, "MISSING_TXN_ID"))
                continue
            currency = row[currency_index]
            if currency not in currencies:
                quarantine.append((source_file, line_number, "UNKNOWN_CURRENCY"))
                continue
            amount_text = row[amount_index]
            if not AMOUNT_RE.match(amount_text) or _fractional_digits(amount_text) > currencies[currency]:
                quarantine.append((source_file, line_number, "INVALID_AMOUNT"))
                continue
            valid.append(row)

    return valid


def _write_csv(path: str, header: list[str], rows: list[list[str]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def _net(members: list[Entry]) -> LedgerSide:
    """Collapse a settlement group into one ledger side."""
    codes = {member.currency for member in members}
    if len(codes) != 1:
        return LedgerSide(currency=None, total=None, count=len(members))
    total = sum((member.amount for member in members), Decimal("0"))
    return LedgerSide(currency=codes.pop(), total=total, count=len(members))


def _classify(
    txn_id: str,
    statement: Entry | None,
    ledger: LedgerSide | None,
    currencies: dict[str, int],
    tolerance: int,
) -> list[str]:
    statement_amount = (
        _format_amount(statement.amount, currencies[statement.currency]) if statement else ""
    )

    if ledger is None:
        assert statement is not None
        return [
            txn_id,
            statement.currency,
            statement_amount,
            "",
            "0",
            "",
            "STATEMENT_ONLY",
        ]

    if ledger.currency is None:
        return [
            txn_id,
            statement.currency if statement else "",
            statement_amount,
            "",
            str(ledger.count),
            "",
            "MIXED_CURRENCY_GROUP",
        ]

    ledger_digits = currencies[ledger.currency]
    ledger_amount = _format_amount(ledger.total, ledger_digits)

    if statement is None:
        return [
            txn_id,
            ledger.currency,
            "",
            ledger_amount,
            str(ledger.count),
            "",
            "LEDGER_ONLY",
        ]

    if statement.currency != ledger.currency:
        return [
            txn_id,
            statement.currency,
            statement_amount,
            ledger_amount,
            str(ledger.count),
            "",
            "CURRENCY_MISMATCH",
        ]

    scaled = abs(statement.amount - ledger.total) * (10 ** ledger_digits)
    difference_minor = int(scaled.to_integral_value())
    status = "MATCHED" if difference_minor <= tolerance else "AMOUNT_MISMATCH"
    return [
        txn_id,
        statement.currency,
        statement_amount,
        ledger_amount,
        str(ledger.count),
        str(difference_minor),
        status,
    ]


def reconcile(input_dir: str, out_dir: str) -> int:
    paths = {name: os.path.join(input_dir, name) for name in REQUIRED_FILES}

    for name, path in paths.items():
        if not os.path.isfile(path):
            print(f"missing required input file: {name}", file=sys.stderr)
            return 2

    try:
        tolerance = _load_tolerance(paths["config.toml"])
        currencies = _load_currencies(paths["currencies.json"])
    except Exception as exc:  # noqa: BLE001 - any config problem is a hard stop
        print(f"invalid configuration: {exc}", file=sys.stderr)
        return 2

    quarantine: list[tuple[str, int, str]] = []

    statement: dict[str, Entry] = {}
    for row in _read_rows(paths["statement.csv"], STATEMENT_FIELDS, 2, 3, currencies, quarantine):
        statement[row[0].strip()] = Entry(row[0].strip(), "", row[2], Decimal(row[3]))

    direct: dict[str, Entry] = {}
    groups: dict[str, list[Entry]] = defaultdict(list)
    for row in _read_rows(paths["ledger.csv"], LEDGER_FIELDS, 2, 3, currencies, quarantine):
        entry = Entry(row[0].strip(), row[1].strip(), row[2], Decimal(row[3]))
        if entry.settlement_ref:
            groups[entry.settlement_ref].append(entry)
        else:
            direct[entry.txn_id] = entry

    ledger_sides: dict[str, LedgerSide] = {
        txn_id: LedgerSide(entry.currency, entry.amount, 1) for txn_id, entry in direct.items()
    }
    for ref, members in groups.items():
        ledger_sides[ref] = _net(members)

    rows = [
        _classify(txn_id, statement.get(txn_id), ledger_sides.get(txn_id), currencies, tolerance)
        for txn_id in sorted(set(statement) | set(ledger_sides))
    ]

    os.makedirs(out_dir, exist_ok=True)
    _write_csv(os.path.join(out_dir, "reconciled.csv"), REPORT_COLUMNS, rows)
    _write_csv(
        os.path.join(out_dir, "quarantine.csv"),
        QUARANTINE_COLUMNS,
        [[source, str(line), reason] for source, line, reason in sorted(quarantine)],
    )
    return 0
PYEOF

python -c "import ast, pathlib; ast.parse(pathlib.Path('/app/reconciler/core.py').read_text())"
