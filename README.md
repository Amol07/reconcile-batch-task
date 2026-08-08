# reconcile-batch-task

A Harbor terminal task in the **processing** category.

A nightly settlement reconciliation job compares a bank statement export against an internal
ledger extract. The version in the container reads neither the tolerance from `config.toml` nor
the minor-unit table from `currencies.json`, assumes every currency has two decimal places, and
aborts the whole batch on the first unparseable row. The agent has to bring it in line with the
specification in `instruction.md`.

What makes it gradeable rather than guessable: the tolerance and the per-currency minor-unit
digits both come from config, so fixtures can vary them and a hardcoded implementation is
distinguishable from a correct one.

## Layout

```
instruction.md              the only thing the agent sees
environment/Dockerfile      builds the broken starting state
environment/project/        the reconciler, bug in place, plus sample data at /app/data
solution/solve.sh           reference fix, rewrites reconciler/core.py
tests/test_outputs.py       hidden tests
tests/test.sh               pytest + reward
```

## Validation

```
harbor run -p reconcile-batch-task --agent oracle
harbor run -p reconcile-batch-task --agent claude-code --model claude-opus-4-8 --n-attempts 5
harbor view jobs
```
