# Fix the nightly settlement reconciliation run

The reconciler in `/app` compares the day's bank statement export against the internal ledger
extract and writes a reconciliation report. Settlement ops say last night's run never produced a
usable report, and that the reports they do get disagree with the balances they reconcile by hand.

Bring the reconciler in line with the specification below. Keep the command line interface as it
is — the run is invoked from `/app` as:

```
python -m reconciler --input-dir <input-dir> --out-dir <out-dir>
```

A sample input directory ships at `/app/data`. It is a sample, not the only input the reconciler
has to handle.

## Inputs

The input directory always contains exactly these four files.

`statement.csv` — the bank export. Header row, then one row per posting:

```
txn_id,posted_at,currency,amount
```

`ledger.csv` — the internal extract. Header row, then:

```
txn_id,settlement_ref,currency,amount
```

`config.toml`:

```toml
[reconciliation]
tolerance_minor_units = 1
```

`currencies.json` — an object mapping ISO currency code to the number of digits that currency
uses for its minor unit. This varies by currency: USD and EUR use 2, JPY uses 0, BHD uses 3.
The set of codes is not fixed; read it from the file.

```json
{"USD": 2, "EUR": 2, "GBP": 2, "JPY": 0, "BHD": 3}
```

Amounts are exact decimal quantities in plain fixed-point notation: optional leading `-`, then
digits, optionally followed by `.` and one or more digits. No thousands separators, no exponent
notation. Negative amounts are normal — a refund offsetting a sale is negative. Amounts must not
lose precision anywhere in the pipeline: high-nominal currencies produce postings with up to
eighteen significant digits, and a single minor unit at that magnitude still has to be reported
exactly, both in a difference and in an echoed amount.

You may assume `txn_id` is unique within each file, that no value appears both as a ledger
`txn_id` and as a ledger `settlement_ref`, that the files contain no blank lines and no quoted
fields, that they are UTF-8, and that line endings are `\n`.

## Row validation

Validate every data row before using it. Apply these checks **in this order** and record the
first one that fails:

| Reason code | Condition |
| --- | --- |
| `MALFORMED_ROW` | The row does not have the number of fields its file's header declares |
| `MISSING_TXN_ID` | `txn_id` is empty or whitespace only |
| `UNKNOWN_CURRENCY` | The currency code is not a key in `currencies.json` |
| `INVALID_AMOUNT` | The amount is not in the format above, or it has more fractional digits than the currency's minor unit allows |

`settlement_ref` may be empty; that is not a validation failure.

A row that fails validation is quarantined and takes no part in reconciliation. The rest of the
run continues.

## Matching

A single bank posting often settles several ledger entries netted together — a batch payout, or a
refund offset against a sale. So ledger entries reach the statement two ways.

**Direct entries** have an empty `settlement_ref`. Each matches the statement row with the same
`txn_id`, one to one.

**Settlement groups** are all the ledger entries sharing the same non-empty `settlement_ref`. The
group as a whole matches the statement row whose `txn_id` equals that `settlement_ref`. A ledger
entry with a non-empty `settlement_ref` never matches on its own `txn_id`.

Grouping happens after validation, so a quarantined entry is simply not a member; the group nets
whichever members remain valid.

For a settlement group the ledger side of the comparison is the **signed sum** of its members'
amounts. The tolerance applies to that netted total, never to individual members.

## Statuses

Each `txn_id` that takes part in reconciliation gets exactly one status.

| Status | Condition |
| --- | --- |
| `MATCHED` | Statement row and ledger side share a currency, and the absolute difference in that currency's minor units is less than or equal to `tolerance_minor_units` |
| `AMOUNT_MISMATCH` | Same currency, difference in minor units greater than the tolerance |
| `CURRENCY_MISMATCH` | Both sides present, ledger side has a single currency, and it differs from the statement currency |
| `MIXED_CURRENCY_GROUP` | A settlement group whose valid members do not all share one currency. No netting is attempted |
| `STATEMENT_ONLY` | Valid statement row with no direct entry and no settlement group for that `txn_id` |
| `LEDGER_ONLY` | A direct entry or settlement group with no valid statement row for that `txn_id` |

Converting a difference into minor units depends on the currency: `0.01` is 1 minor unit in USD,
`1` is 1 minor unit in JPY.

## Outputs

Write both files into the output directory, creating it if needed. Both are always written, even
when they have no data rows. Use `\n` line endings and no quoting.

`reconciled.csv`, with exactly this header:

```
txn_id,currency,statement_amount,ledger_amount,ledger_entry_count,difference_minor,status
```

- One row per `txn_id` that took part in reconciliation, sorted by `txn_id` ascending (plain
  lexicographic order). A settlement group produces one row, not one per member.
- `currency` is the statement row's currency when there is a valid statement row, otherwise the
  ledger side's currency. For a `MIXED_CURRENCY_GROUP` with no valid statement row it is empty.
- `statement_amount` is fixed-point with exactly as many fractional digits as the statement
  currency's minor unit, and empty when there is no valid statement row.
- `ledger_amount` is the direct entry's amount or the group's netted total, formatted with the
  ledger currency's minor-unit digits. It is empty when there is no ledger side, and empty for
  `MIXED_CURRENCY_GROUP`.
- `ledger_entry_count` is how many valid ledger entries the ledger side is made of: `1` for a
  direct entry, the member count for a settlement group, `0` for `STATEMENT_ONLY`.
- `difference_minor` is a non-negative integer with no sign and no decimal point. It is empty for
  `CURRENCY_MISMATCH`, `MIXED_CURRENCY_GROUP`, `STATEMENT_ONLY`, and `LEDGER_ONLY`.

`quarantine.csv`, with exactly this header:

```
source_file,line_number,reason
```

- One row per quarantined input row.
- `source_file` is the file's name only — `statement.csv` or `ledger.csv` — not a path.
- `line_number` is the 1-based line number within that file, counting the header as line 1. The
  first data row is therefore line 2.
- `reason` is one of the four reason codes above.
- Sorted by `source_file` ascending, then by `line_number` ascending.

## Exit status

- `0` when the run completes, including when rows were quarantined.
- `2` when a required input file is missing, or `config.toml` or `currencies.json` cannot be
  parsed, or `tolerance_minor_units` is absent or is not an integer. In that case write no output
  files and print a message to stderr.
