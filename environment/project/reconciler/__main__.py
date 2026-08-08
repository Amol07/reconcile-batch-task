"""Command line entry point for the nightly reconciliation run."""
from __future__ import annotations

import argparse
import sys

from .core import reconcile


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="reconciler",
        description="Reconcile a bank statement export against the internal ledger extract.",
    )
    parser.add_argument("--input-dir", required=True, help="directory holding the day's inputs")
    parser.add_argument("--out-dir", required=True, help="directory to write the report into")
    args = parser.parse_args(argv)
    return reconcile(args.input_dir, args.out_dir)


if __name__ == "__main__":
    sys.exit(main())
