"""Hidden tests for the settlement reconciliation task.

Every test builds its own input directory, runs the CLI from /app, and asserts
on the files it wrote.
"""
from __future__ import annotations

import csv
import json
import subprocess
from pathlib import Path

PROJECT_DIR = Path("/app")
CURRENCIES = {"USD": 2, "EUR": 2, "GBP": 2, "JPY": 0, "BHD": 3}
TS = "2026-05-04T09:00:00Z"

REPORT_HEADER = (
    "txn_id,currency,statement_amount,ledger_amount,ledger_entry_count,difference_minor,status"
)
QUARANTINE_HEADER = "source_file,line_number,reason"


def write_inputs(base, statement_rows, ledger_rows, tolerance=0, currencies=None):
    base.mkdir(parents=True, exist_ok=True)
    (base / "statement.csv").write_text(
        "txn_id,posted_at,currency,amount\n" + "".join(row + "\n" for row in statement_rows),
        encoding="utf-8",
    )
    (base / "ledger.csv").write_text(
        "txn_id,settlement_ref,currency,amount\n" + "".join(row + "\n" for row in ledger_rows),
        encoding="utf-8",
    )
    (base / "config.toml").write_text(
        f"[reconciliation]\ntolerance_minor_units = {tolerance}\n", encoding="utf-8"
    )
    (base / "currencies.json").write_text(
        json.dumps(CURRENCIES if currencies is None else currencies), encoding="utf-8"
    )
    return base


def run(input_dir, out_dir):
    return subprocess.run(
        [
            "python3",
            "-m",
            "reconciler",
            "--input-dir",
            str(input_dir),
            "--out-dir",
            str(out_dir),
        ],
        cwd=PROJECT_DIR,
        capture_output=True,
        text=True,
        timeout=180,
    )


def run_ok(input_dir, out_dir):
    result = run(input_dir, out_dir)
    assert result.returncode == 0, f"exit {result.returncode}, stderr: {result.stderr}"
    return result


def report(out_dir):
    with (out_dir / "reconciled.csv").open(newline="", encoding="utf-8") as fh:
        return {row["txn_id"]: row for row in csv.DictReader(fh)}


def quarantine(out_dir):
    with (out_dir / "quarantine.csv").open(newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    return rows[0], rows[1:]


# --- configuration and per-currency handling ---------------------------------


def test_tolerance_is_read_from_config(tmp_path):
    """The same 3-minor-unit gap mismatches at tolerance 0 and matches at tolerance 5."""
    statement = [f"T1,{TS},USD,10.00", f"T2,{TS},USD,20.00"]
    ledger = ["T1,,USD,10.03", "T2,,USD,20.00"]

    strict_out = tmp_path / "strict-out"
    loose_out = tmp_path / "loose-out"
    run_ok(write_inputs(tmp_path / "strict", statement, ledger, tolerance=0), strict_out)
    run_ok(write_inputs(tmp_path / "loose", statement, ledger, tolerance=5), loose_out)

    assert report(strict_out)["T1"]["status"] == "AMOUNT_MISMATCH"
    assert report(strict_out)["T1"]["difference_minor"] == "3"
    assert report(loose_out)["T1"]["status"] == "MATCHED"
    assert report(loose_out)["T1"]["difference_minor"] == "3"
    assert report(strict_out)["T2"]["status"] == "MATCHED"


def test_minor_units_follow_the_currency(tmp_path):
    """A one-minor-unit gap in JPY and BHD is one unit, and amounts keep each currency's digits."""
    statement = [f"J1,{TS},JPY,15000", f"B1,{TS},BHD,12.345", f"U1,{TS},USD,10.00"]
    ledger = ["J1,,JPY,15001", "B1,,BHD,12.344", "U1,,USD,10.01"]
    out = tmp_path / "out"
    run_ok(write_inputs(tmp_path / "in", statement, ledger, tolerance=0), out)
    rows = report(out)

    assert rows["J1"]["difference_minor"] == "1"
    assert rows["J1"]["statement_amount"] == "15000"
    assert rows["J1"]["ledger_amount"] == "15001"
    assert rows["B1"]["difference_minor"] == "1"
    assert rows["B1"]["statement_amount"] == "12.345"
    assert rows["U1"]["difference_minor"] == "1"
    assert rows["U1"]["statement_amount"] == "10.00"
    assert {rows[k]["status"] for k in ("J1", "B1", "U1")} == {"AMOUNT_MISMATCH"}


def test_larger_tolerance_spans_currencies(tmp_path):
    """At tolerance 2 a 2-unit JPY gap matches while a 3-unit BHD gap does not."""
    statement = [f"J1,{TS},JPY,900", f"B1,{TS},BHD,5.000"]
    ledger = ["J1,,JPY,902", "B1,,BHD,5.003"]
    out = tmp_path / "out"
    run_ok(write_inputs(tmp_path / "in", statement, ledger, tolerance=2), out)
    rows = report(out)

    assert rows["J1"]["status"] == "MATCHED"
    assert rows["J1"]["difference_minor"] == "2"
    assert rows["B1"]["status"] == "AMOUNT_MISMATCH"
    assert rows["B1"]["difference_minor"] == "3"


def test_unlisted_currency_digits_are_honoured(tmp_path):
    """Minor unit digits come from currencies.json, including codes absent from the sample."""
    currencies = dict(CURRENCIES, KWD=3, CLP=0)
    statement = [f"K1,{TS},KWD,1.500", f"C1,{TS},CLP,9500"]
    ledger = ["K1,,KWD,1.500", "C1,,CLP,9500"]
    out = tmp_path / "out"
    run_ok(
        write_inputs(tmp_path / "in", statement, ledger, tolerance=0, currencies=currencies), out
    )
    rows = report(out)

    assert rows["K1"]["statement_amount"] == "1.500"
    assert rows["C1"]["statement_amount"] == "9500"
    assert rows["K1"]["status"] == "MATCHED"
    assert rows["C1"]["status"] == "MATCHED"


# --- settlement group netting ------------------------------------------------


def test_group_nets_signed_amounts(tmp_path):
    """A group containing a refund nets to the signed sum, not the sum of magnitudes."""
    statement = [f"B1,{TS},USD,430.00"]
    ledger = ["L1,B1,USD,500.00", "L2,B1,USD,-75.50", "L3,B1,USD,5.50"]
    out = tmp_path / "out"
    run_ok(write_inputs(tmp_path / "in", statement, ledger, tolerance=0), out)
    row = report(out)["B1"]

    assert row["status"] == "MATCHED"
    assert row["ledger_amount"] == "430.00"
    assert row["ledger_entry_count"] == "3"
    assert row["difference_minor"] == "0"


def test_group_members_are_not_reported_individually(tmp_path):
    """A settlement group yields one report row, keyed by the ref, not one row per member."""
    statement = [f"B1,{TS},USD,60.00"]
    ledger = ["L1,B1,USD,40.00", "L2,B1,USD,20.00"]
    out = tmp_path / "out"
    run_ok(write_inputs(tmp_path / "in", statement, ledger, tolerance=0), out)
    rows = report(out)

    assert set(rows) == {"B1"}
    assert rows["B1"]["ledger_entry_count"] == "2"


def test_tolerance_applies_to_the_net_not_the_members(tmp_path):
    """A group whose members are individually far off still matches when the net is within tolerance."""
    statement = [f"B1,{TS},USD,100.00"]
    ledger = ["L1,B1,USD,900.00", "L2,B1,USD,-800.01"]
    out = tmp_path / "out"
    run_ok(write_inputs(tmp_path / "in", statement, ledger, tolerance=1), out)
    row = report(out)["B1"]

    assert row["ledger_amount"] == "99.99"
    assert row["difference_minor"] == "1"
    assert row["status"] == "MATCHED"


def test_group_netting_uses_currency_minor_units(tmp_path):
    """Netted groups in JPY and BHD compute differences in their own minor units."""
    statement = [f"BJ,{TS},JPY,8800", f"BB,{TS},BHD,1.000"]
    ledger = [
        "L1,BJ,JPY,9000",
        "L2,BJ,JPY,-199",
        "L3,BB,BHD,1.500",
        "L4,BB,BHD,-0.498",
    ]
    out = tmp_path / "out"
    run_ok(write_inputs(tmp_path / "in", statement, ledger, tolerance=0), out)
    rows = report(out)

    assert rows["BJ"]["ledger_amount"] == "8801"
    assert rows["BJ"]["difference_minor"] == "1"
    assert rows["BB"]["ledger_amount"] == "1.002"
    assert rows["BB"]["difference_minor"] == "2"


def test_mixed_currency_group(tmp_path):
    """A group whose members disagree on currency is flagged and not netted."""
    statement = [f"B1,{TS},USD,60.00"]
    ledger = ["L1,B1,USD,40.00", "L2,B1,EUR,20.00"]
    out = tmp_path / "out"
    run_ok(write_inputs(tmp_path / "in", statement, ledger, tolerance=0), out)
    row = report(out)["B1"]

    assert row["status"] == "MIXED_CURRENCY_GROUP"
    assert row["ledger_amount"] == ""
    assert row["difference_minor"] == ""
    assert row["ledger_entry_count"] == "2"
    assert row["currency"] == "USD"


def test_group_with_no_statement_row(tmp_path):
    """An orphaned settlement group reports as ledger-only with its netted total."""
    statement = [f"S1,{TS},USD,5.00"]
    ledger = ["S1,,USD,5.00", "L1,B9,USD,15.00", "L2,B9,USD,-3.00"]
    out = tmp_path / "out"
    run_ok(write_inputs(tmp_path / "in", statement, ledger, tolerance=0), out)
    row = report(out)["B9"]

    assert row["status"] == "LEDGER_ONLY"
    assert row["ledger_amount"] == "12.00"
    assert row["ledger_entry_count"] == "2"
    assert row["statement_amount"] == ""


def test_grouped_entry_does_not_match_on_its_own_txn_id(tmp_path):
    """A ledger entry with a settlement_ref is invisible to a statement row sharing its txn_id."""
    statement = [f"L1,{TS},USD,40.00", f"B1,{TS},USD,40.00"]
    ledger = ["L1,B1,USD,40.00"]
    out = tmp_path / "out"
    run_ok(write_inputs(tmp_path / "in", statement, ledger, tolerance=0), out)
    rows = report(out)

    assert rows["L1"]["status"] == "STATEMENT_ONLY"
    assert rows["L1"]["ledger_entry_count"] == "0"
    assert rows["B1"]["status"] == "MATCHED"
    assert rows["B1"]["ledger_entry_count"] == "1"


def test_quarantined_member_leaves_the_group_short(tmp_path):
    """An invalid member drops out of its group, and the rest still net."""
    statement = [f"B1,{TS},USD,100.00"]
    ledger = ["L1,B1,USD,60.00", "L2,B1,USD,fifty", "L3,B1,USD,40.00"]
    out = tmp_path / "out"
    run_ok(write_inputs(tmp_path / "in", statement, ledger, tolerance=0), out)
    row = report(out)["B1"]

    assert row["ledger_amount"] == "100.00"
    assert row["ledger_entry_count"] == "2"
    assert row["status"] == "MATCHED"
    _, bad = quarantine(out)
    assert bad == [["ledger.csv", "3", "INVALID_AMOUNT"]]


def test_single_member_group_counts_as_one(tmp_path):
    """A group of one still nets and reports a count of one."""
    statement = [f"B1,{TS},USD,25.00"]
    ledger = ["L1,B1,USD,25.00"]
    out = tmp_path / "out"
    run_ok(write_inputs(tmp_path / "in", statement, ledger, tolerance=0), out)
    row = report(out)["B1"]

    assert row["status"] == "MATCHED"
    assert row["ledger_entry_count"] == "1"


# --- validation, one-sided rows, output shape --------------------------------


def test_quarantined_rows_are_recorded_with_line_numbers(tmp_path):
    """Bad rows land in quarantine.csv with the right reason, file and line, and the run survives."""
    statement = [
        f"Q1,{TS},USD,ninety",
        f"Q2,{TS},XYZ,10.00",
        f",{TS},USD,10.00",
        f"Q4,{TS},USD",
        f"Q5,{TS},JPY,10.5",
        f"Q6,{TS},USD,10.00",
    ]
    ledger = [
        "Q1,,USD,10.00",
        "Q2,,USD,10.00",
        "Q4,,USD,10.00",
        "Q5,,JPY,10",
        "Q6,,USD,10.00",
        "Q7,,USD,abc",
    ]
    out = tmp_path / "out"
    run_ok(write_inputs(tmp_path / "in", statement, ledger, tolerance=0), out)

    header, rows = quarantine(out)
    assert header == QUARANTINE_HEADER.split(",")
    assert rows == [
        ["ledger.csv", "7", "INVALID_AMOUNT"],
        ["statement.csv", "2", "INVALID_AMOUNT"],
        ["statement.csv", "3", "UNKNOWN_CURRENCY"],
        ["statement.csv", "4", "MISSING_TXN_ID"],
        ["statement.csv", "5", "MALFORMED_ROW"],
        ["statement.csv", "6", "INVALID_AMOUNT"],
    ]


def test_quarantined_row_leaves_its_counterpart_one_sided(tmp_path):
    """When one side is quarantined the valid counterpart is reported as present on one side."""
    statement = [f"Q1,{TS},USD,ninety", f"Q6,{TS},USD,10.00", f"S1,{TS},USD,5.00"]
    ledger = ["Q1,,USD,10.00", "Q6,,USD,10.00"]
    out = tmp_path / "out"
    run_ok(write_inputs(tmp_path / "in", statement, ledger, tolerance=0), out)
    rows = report(out)

    assert rows["Q1"]["status"] == "LEDGER_ONLY"
    assert rows["Q1"]["statement_amount"] == ""
    assert rows["Q1"]["ledger_amount"] == "10.00"
    assert rows["Q1"]["difference_minor"] == ""
    assert rows["S1"]["status"] == "STATEMENT_ONLY"
    assert rows["S1"]["ledger_amount"] == ""
    assert rows["S1"]["ledger_entry_count"] == "0"
    assert rows["Q6"]["status"] == "MATCHED"


def test_currency_mismatch_formats_each_side_in_its_own_currency(tmp_path):
    """A currency mismatch reports the statement currency and formats each amount separately."""
    statement = [f"C1,{TS},JPY,100"]
    ledger = ["C1,,USD,100.00"]
    out = tmp_path / "out"
    run_ok(write_inputs(tmp_path / "in", statement, ledger, tolerance=0), out)
    row = report(out)["C1"]

    assert row["status"] == "CURRENCY_MISMATCH"
    assert row["currency"] == "JPY"
    assert row["statement_amount"] == "100"
    assert row["ledger_amount"] == "100.00"
    assert row["difference_minor"] == ""


def test_report_header_and_ordering(tmp_path):
    """The report carries the exact header and is sorted by txn_id regardless of input order."""
    statement = [f"T-30,{TS},USD,1.00", f"T-10,{TS},USD,1.00", f"T-20,{TS},USD,1.00"]
    ledger = ["T-20,,USD,1.00", "T-30,,USD,1.00", "T-10,,USD,1.00"]
    out = tmp_path / "out"
    run_ok(write_inputs(tmp_path / "in", statement, ledger, tolerance=0), out)

    lines = (out / "reconciled.csv").read_text(encoding="utf-8").splitlines()
    assert lines[0] == REPORT_HEADER
    assert [line.split(",")[0] for line in lines[1:]] == ["T-10", "T-20", "T-30"]


def test_clean_run_still_writes_an_empty_quarantine_file(tmp_path):
    """quarantine.csv is written with just its header when nothing was quarantined."""
    out = tmp_path / "out"
    run_ok(write_inputs(tmp_path / "in", [f"T1,{TS},USD,1.00"], ["T1,,USD,1.00"], tolerance=0), out)

    assert (out / "quarantine.csv").read_text(encoding="utf-8") == QUARANTINE_HEADER + "\n"


def test_missing_input_file_exits_two_without_writing_output(tmp_path):
    """A missing required input is a hard stop: exit 2 and no report on disk."""
    input_dir = write_inputs(tmp_path / "in", [f"T1,{TS},USD,1.00"], ["T1,,USD,1.00"], tolerance=0)
    (input_dir / "config.toml").unlink()
    out = tmp_path / "out"

    result = run(input_dir, out)
    assert result.returncode == 2
    assert not (out / "reconciled.csv").exists()


def test_unparseable_currencies_file_exits_two(tmp_path):
    """A corrupt currencies.json is a hard stop rather than a partial run."""
    input_dir = write_inputs(tmp_path / "in", [f"T1,{TS},USD,1.00"], ["T1,,USD,1.00"], tolerance=0)
    (input_dir / "currencies.json").write_text("{not json", encoding="utf-8")
    out = tmp_path / "out"

    result = run(input_dir, out)
    assert result.returncode == 2
    assert not (out / "reconciled.csv").exists()
