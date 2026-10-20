---
name: run-tests
description: Run the Aegis Liquidations test suite (pytest unit tests and optional integration tests against PostgreSQL and an anvil fork) and report results. Use when asked to run tests or after implementing a change.
---

# run-tests

Arguments (optional): a test path, `-k` expression, or `integration` / `all`.

1. Unit tests (default):
   ```
   make test ARGS="<path or -k expr>"
   ```
2. `integration` or `all`: `make test-integration`. It needs PostgreSQL on `127.0.0.1:5433` (`make up`) and, for the fork test, `anvil` plus `FORK_URL_OPTIMISM`. If a prerequisite is missing, say which tests were skipped instead of pretending they passed.
3. Report: passed / failed / skipped counts per suite. For each failure, paste the assertion and traceback tail verbatim and point to `file:line`.
