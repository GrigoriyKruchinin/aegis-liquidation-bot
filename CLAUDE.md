# CLAUDE.md — Aegis Liquidations (`aegis-liquidation-bot`)

Multi-chain liquidation bot platform for EVM L2 networks. Strictly typed, async, SOLID, fully dockerized.

## Language Policy

- **Code, comments, docstrings, commit messages, docs, `README.md`, `todo.md`, agent/skill files:** English.
- **All interactive communication with the user (questions, guides, summaries):** Russian.

## Roadmap Summary (see `todo.md` for the full checklist)

| Version | Scope | Mode |
|---|---|---|
| V1 | Opportunity detector + Telegram alerts | Read-only, local (`docker-compose.local.yml`) |
| V2 | Smart contracts, flash loans, Dynamic Liquidity Router, on-chain execution, CI/CD, reports | Active, VPS (`docker-compose.yml`) |
| V3 | FastAPI admin dashboard (auth, analytics, Chart.js), multi-chain plugins | VPS |

### Confirmed V1 Parameters

- Networks: **Base, Arbitrum, Optimism**.
- Protocols: **Aave v3** (all three chains) and **Compound v3 (Comet)**.
- `MIN_PROFIT_USD=5` — minimum net profit (USD) to trigger a Telegram alert.
- Admin dashboard credentials (V3): `ADMIN_USERNAME` + `ADMIN_PASSWORD_HASH` (bcrypt) in `.env`. Never commit real values; `.env.example` holds placeholders only.

## Tech Stack

- Python 3.12, dependency management with **`uv`** (`pyproject.toml`, `uv.lock`).
- `pydantic` v2 + `pydantic-settings` for config and validation.
- `web3.py` (AsyncWeb3) + Multicall3 for batched RPC reads.
- PostgreSQL + SQLAlchemy 2.x (AsyncIO, `asyncpg`) + Alembic.
- Redis + Celery (scan scheduling via beat, notification queue).
- Telegram Bot API via `httpx` (HTML parse mode).
- FastAPI + Jinja2 + Chart.js (V3).
- Solidity + Foundry (V2).
- Quality: `ruff` (lint + format), `mypy --strict`, `pytest` + `pytest-asyncio`.

## Planned Layout

```
src/aegis/
  config/      # pydantic-settings models, per-chain config
  core/        # interfaces (ABCs/Protocols), domain models, DI container
  chains/      # BaseChainProvider implementations (base, arbitrum, optimism)
  protocols/   # BaseLendingProtocol implementations (aave_v3, compound_v3)
  scanner/     # IHealthFactorScanner, borrower discovery / indexing
  estimator/   # profit & gas estimation (incl. L1 data fee on OP-stack/Arbitrum)
  executor/    # V2: Dynamic Liquidity Router, tx builder, flash loan adapters
  notifier/    # INotifier implementations, Telegram templates
  workers/     # Celery app, tasks, beat schedule
  db/          # SQLAlchemy models, repositories, session factory
  api/         # V3: FastAPI dashboard
contracts/     # V2: Foundry project
migrations/    # Alembic
tests/{unit,integration}/
```

## Commands (root `Makefile`)

| Command | Purpose |
|---|---|
| `make install` | `uv sync` (all groups) |
| `make format` | `uv run ruff format . && uv run ruff check --fix .` |
| `make format-check` | `uv run ruff format --check .` |
| `make lint` | `uv run ruff check .` |
| `make typecheck` | `uv run mypy src tests` (strict) |
| `make test` | `uv run pytest` (pass `ARGS=...` for path/marker) |
| `make check` | format-check + lint + typecheck + test |
| `make migrate` | `uv run alembic upgrade head` |
| `make up-local` / `make down-local` | `docker compose -f docker-compose.local.yml up -d --build` / `down` |
| `make logs-local` | follow local compose logs |
| `make contracts-test` | V2: `cd contracts && forge test -vvv` |

Integration tests that hit a forked chain (anvil) are marked `@pytest.mark.integration` and skipped by default unless `RUN_INTEGRATION=1`.

## Code Style & Architecture Rules

1. **SOLID is mandatory.**
   - SRP: Scanner, Estimator, Executor, Celery Worker, Notifier, Dashboard are separate modules with no cross-responsibility.
   - OCP / Strategy: a new chain or lending protocol = a new class implementing `BaseChainProvider` / `BaseLendingProtocol` + a registry entry. **Never** edit the orchestrator to add one.
   - LSP / ISP: narrow interfaces — `IFlashLoanProvider`, `INotifier`, `IHealthFactorScanner`, `IPriceOracle`, `IGasEstimator`, repositories.
   - DIP: high-level modules depend on interfaces; wiring happens only in the DI container / factories (`core/container.py`).
2. **Typing:** every function fully annotated; `mypy --strict` must pass. Prefer `Protocol`/`ABC` for interfaces, frozen pydantic models or `@dataclass(frozen=True, slots=True)` for domain values.
3. **Money & amounts:** on-chain amounts are `int` (wei / token base units); USD values are `Decimal`. **Never use `float` for money.**
4. **Async:** all I/O (RPC, DB, Redis, HTTP) is async. No blocking calls in the event loop. Batch reads via Multicall3; avoid N+1 RPC calls.
5. **Secrets:** loaded only via `pydantic-settings` (`SecretStr`). Never log, print, or include secrets in exceptions/notifications. V1 loads **no private keys**.
6. **Telegram:** HTML parse mode, all dynamic values escaped (`html.escape`), templates live in `notifier/templates`. Use emojis, `<b>` highlights, separators, `<blockquote>`, `<code>` for addresses/hashes.
7. **Logging:** structured (`structlog` or stdlib JSON), include `chain`, `protocol`, `borrower`, `opportunity_id` context.
8. **Errors:** specific exception classes in `core/errors.py`; no bare `except`. RPC calls wrapped with retries + timeouts.
9. **Tests:** every new module ships with unit tests; external services mocked behind interfaces.
10. Ruff line length 100; imports sorted by ruff; docstrings in Google style for public APIs.

## Safety Invariants

- V1 is strictly read-only: no signer, no `eth_sendRawTransaction`.
- V2: `ALLOW_FLASH_LOANS: bool` is a hard kill switch — when `False`, no flash-loan path is ever built.
- Dynamic Liquidity Router: self-fund if hot wallet balance ≥ required debt → else flash loan only if allowed AND still profitable after fee → else skip and log the reason.
- Every transaction is simulated (`eth_call`) before sending; contracts are owner-only.

## Workflow

1. Pick the next unchecked task from `todo.md`.
2. Implement via the **implementation-agent** (`.claude/agents/implementation-agent.md`) conventions.
3. Run `/check-code-quality` and `/run-tests`.
4. For anything touching keys, RPC, money math, contracts, or execution, run `/audit-security` (uses **security-critic-agent**).
5. Tick the box in `todo.md`. Commit only when the user asks.

Before starting V1 implementation, give the user (in Russian) the Telegram BotFather / `TELEGRAM_CHAT_ID` / local `.env` guide. Before V2, give the VPS / SSH / Docker / deploy keys / gas wallet guide.
