# CLAUDE.md — Aegis Liquidations (`aegis-liquidation-bot`)

Read-only liquidation analytics for EVM L2 lending markets, with Telegram notifications.
Strictly typed, async, SOLID, fully dockerized.

## Language Policy

- **Code, comments, docstrings, commit messages, docs, `README.md`, agent/skill files:** English.
- **All interactive communication with the user (questions, guides, summaries):** Russian.

## What the System Does

- **Networks:** Base, Arbitrum One, Optimism.
- **Protocols:** Aave v3 and Compound v3 (Comet).
- **Position monitoring:** discovers borrowers from events, re-checks health factors (full scans + a per-block watcher for near-liquidation positions) and alerts on liquidatable positions whose estimated net profit is at least `MIN_PROFIT_USD`.
- **Market analytics:** indexes every liquidation on the monitored markets, values it at its block (debt, collateral, gas incl. L1 data fee), measures liquidator speed (blocks between "liquidatable" and "liquidated") and detector coverage.
- **Telegram:** liquidatable position alerts, 🐋 large liquidation alerts, the 📈 daily market digest and the 📊 hourly status report.
- **Reports:** `make market-report DAYS=N` → Markdown / JSON / CSV in `data/market/`.

## Tech Stack

- Python 3.12, dependency management with **`uv`** (`pyproject.toml`, `uv.lock`).
- `pydantic` v2 + `pydantic-settings` for config and validation.
- `web3.py` (AsyncWeb3) + Multicall3 for batched RPC reads.
- PostgreSQL + SQLAlchemy 2.x (AsyncIO, `asyncpg`) + Alembic.
- Redis + Celery (beat schedule, queues `scan`, `market`, `enrich`, `notify`).
- Telegram Bot API via `httpx` (HTML parse mode).
- Quality: `ruff` (lint + format), `mypy --strict`, `pytest` + `pytest-asyncio`; `anvil` (Foundry) for fork tests.

## Layout

```
src/aegis/
  config/      # pydantic-settings groups, static chain / market registry
  core/        # interfaces (ABCs/Protocols), domain models, errors, DI container
  chains/      # chain providers (generic EVM, OP-stack, Arbitrum), RPC budget, Multicall3
  protocols/   # BaseLendingProtocol implementations (aave_v3, compound_v3)
  scanner/     # event indexers, health-factor scanner, orchestrator, per-block watcher
  estimator/   # profit & gas estimation (incl. L1 data fee), Chainlink native price
  market/      # liquidation stream, enrichment (valuation, gas, lag), lag search
  reporting/   # status report and daily market digest builders
  notifier/    # Telegram notifier and HTML templates
  workers/     # Celery app, tasks, beat schedule, Redis tools, watcher entry point
  db/          # SQLAlchemy models, repositories, session factory
  tools/       # CLI tools: market report, RPC latency
migrations/    # Alembic
tests/{unit,integration}/
```

## Commands (root `Makefile`)

| Command | Purpose |
|---|---|
| `make install` | `uv sync` (all groups) |
| `make format` | `ruff format` + `ruff check --fix` |
| `make format-check` / `make lint` | formatting / lint checks |
| `make typecheck` | `uv run mypy src tests` (strict) |
| `make test` | unit tests (`ARGS=...` for path / `-k`) |
| `make test-integration` | DB repository tests + anvil fork test (`RUN_INTEGRATION=1`) |
| `make check` | format-check + lint + typecheck + test |
| `make migrate` | `alembic upgrade head` |
| `make up` / `make down` / `make ps` / `make logs` | Docker Compose stack |
| `make market-report DAYS=N` | liquidation market report into `data/market/` |
| `make rpc-latency` | compare configured RPC endpoints |

Integration tests are marked `@pytest.mark.integration` and skipped unless `RUN_INTEGRATION=1`. They use an isolated `<POSTGRES_DB>_test` database on `127.0.0.1:5433` and never touch live data.

## Code Style & Architecture Rules

1. **SOLID is mandatory.**
   - SRP: indexing, scanning, estimation, enrichment, reporting, notification and Celery wiring are separate modules.
   - OCP / Strategy: a new chain or lending protocol = a new class implementing `BaseChainProvider` / `BaseLendingProtocol` + a registry entry. **Never** edit the orchestrator to add one.
   - LSP / ISP: narrow interfaces — `INotifier`, `INotificationDispatcher`, `ILiquidationAlertDispatcher`, `IHealthFactorScanner`, `IPriceOracle`, `IGasEstimator`, `IRateLimiter`, repositories.
   - DIP: high-level modules depend on interfaces; wiring happens only in `core/container.py`.
2. **Typing:** every function fully annotated; `mypy --strict` must pass. Prefer `Protocol`/`ABC` for interfaces and `@dataclass(frozen=True, slots=True)` for domain values.
3. **Money & amounts:** on-chain amounts are `int` (base units); USD values are `Decimal`. **Never use `float` for money.**
4. **Async:** all I/O (RPC, DB, Redis, HTTP) is async. No blocking calls in the event loop. Batch reads via Multicall3; avoid N+1 RPC calls.
5. **RPC budget:** every request goes through the provider's `_request` (retries, timeouts, failover, compute-unit limiter). `eth_getLogs` prefers the log-only endpoints.
6. **Secrets:** loaded only via `pydantic-settings` (`SecretStr`). Never log, print, or include secrets in exceptions/notifications; use `aegis.core.redact`.
7. **Telegram:** HTML parse mode, every dynamic value escaped with `esc`, templates live in `notifier/templates`. Use emojis, `<b>` highlights, separators, `<blockquote>`, `<code>` for addresses/hashes.
8. **Logging:** structured JSON (`structlog`), with `chain`, `protocol`, `market`, `borrower` context.
9. **Errors:** specific exception classes in `core/errors.py`; no bare `except`.
10. **Database changes** ship as a new Alembic migration; never edit an applied one.
11. **Tests:** every change ships with unit tests; external services are mocked behind interfaces.
12. Ruff line length 100; imports sorted by ruff; Google-style docstrings for public APIs.

## Safety Invariants

- The system is strictly read-only: no private keys, no signer, no `eth_sendRawTransaction`.
- Historical backfills never trigger real-time alerts (`MARKET_ALERT_MAX_AGE_MINUTES`).
- Notifications are idempotent (Redis sent-markers) and deduplicated per position.

## Workflow

1. A new feature or a bug fix is implemented by the **developer-agent** (`.claude/agents/developer-agent.md`).
2. Run `/check-code-quality` and `/run-tests`.
3. Changes touching RPC, secrets, money math, the database or notifications are reviewed by the **security-critic-agent** via `/audit-security`.
4. Commit only when the user asks.
