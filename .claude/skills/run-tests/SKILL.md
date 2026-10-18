---
name: run-tests
description: Run the Aegis Liquidations test suite (pytest unit tests, optional integration tests on an anvil fork, and Foundry contract tests) and report results. Use when asked to run tests or after implementing a task.
---

# run-tests

Arguments (optional): a test path, `-k` expression, or `integration` / `contracts` / `all`.

1. Unit tests (default):
   ```
   make test ARGS="<path or -k expr>"
   ```
   Fallback without Makefile: `uv run pytest <args>`.
2. `integration` or `all`: run `RUN_INTEGRATION=1 uv run pytest -m integration`. Requires `anvil` and fork RPC URLs in `.env`; if missing, say so instead of pretending it passed.
3. `contracts` or `all` (only if `contracts/` exists): `make contracts-test` (`forge test -vvv`).
4. Report: passed / failed / skipped counts per suite. For each failure, paste the assertion and traceback tail verbatim and point to `file:line`. Do not mark a `todo.md` item done while tests fail.
