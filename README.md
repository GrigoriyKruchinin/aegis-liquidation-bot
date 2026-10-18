# Aegis Liquidations

Multi-chain liquidation bot platform for EVM L2 networks. Strictly typed, async, SOLID and fully dockerized.

| Version | Scope | Status |
|---|---|---|
| **V1** | Read-only opportunity detector + Telegram alerts (Base, Arbitrum, Optimism · Aave v3, Compound v3) | ✅ implemented |
| V2 | Smart contracts, flash loans, Dynamic Liquidity Router, on-chain execution, CI/CD | planned |
| V3 | FastAPI admin dashboard, analytics, multi-chain plugins | planned |

See [`todo.md`](todo.md) for the full roadmap.

## How V1 works

```
                ┌──────────────┐  60s / 30s     ┌──────────────────────────┐
                │ celery-beat  │ ─────────────▶ │ scanner (Celery, -Q scan) │
                └──────────────┘                │ index : eth_getLogs → borrowers
                                                │ full  : HF of all active borrowers
                                                └──────────┬───────────────┘
                ┌──────────────────────────┐               │
                │ watcher (asyncio, 1/chain)│ every block:  │
                │ • all oracle prices (1 multicall) → move ≥ 0.5% ⇒ immediate full rescan
                │ • HF < 1.05 hot set re-check  │           │
                └──────────────┬───────────┘               │
              Multicall3 batched eth_call                  │ HF < 1 & net profit ≥ MIN_PROFIT_USD
   Base / Arbitrum / Optimism RPC  ◀──────────────────────┤ Redis dedup (SET NX EX)
                                                           ▼
   ┌──────────┐   opportunities, borrowers,   ┌─────────────────────────┐   HTML message
   │ Postgres │ ◀──── checkpoints ────────────│ notifier-worker (-Q notify) ─────────▶ Telegram
   └──────────┘                               └─────────────────────────┘
```

1. **Borrower discovery.** The indexer scans `Borrow` events (Aave v3) and base `Withdraw` events (Comet) in adaptive `eth_getLogs` chunks. It checkpoints the last block for each market and stays `reorg_safety_blocks` behind the chain head.
2. **Health scan.** The scanner batches `getUserAccountData` (Aave) and `isLiquidatable` + balances (Comet) through Multicall3. It stores the latest HF for each borrower. A full scan of all active borrowers runs every `SCAN_INTERVAL_SECONDS`.
3. **Fast watcher (detection latency).** A dedicated asyncio service runs one loop per chain, synchronised with block production. After each block it sleeps about one block interval and then polls the head every `WATCHER_HEAD_POLL_MS` until the next block appears. On every new block it does two things concurrently:
   - Reads every oracle price of all markets on the chain in **one** Multicall3 request. If any price has moved by `WATCHER_PRICE_MOVE_BPS` or more since the last snapshot, a full rescan of that market starts at once in the background.
   - Re-checks the hot set: borrowers with an HF below `WATCHLIST_HF_THRESHOLD`.

   The effective tick is `max(block time, WATCHER_MIN_INTERVAL_MS)`. That is every block on Base/Optimism (2 s) and 1 s on Arbitrum by default. Lowering it on Arbitrum costs more RPC requests. Run `make rpc-latency` to compare how fresh your RPC endpoints are. The freshest node should come first, because a node that serves new blocks late caps detection speed.
4. **Profit estimate (self-funded).**
   - **Aave v3 (v3.3 rules):**
     - The close factor is 50% of the *total* debt. It rises to 100% of the reserve debt when HF < 0.95 or a leg is below $2k.
     - Partial liquidations must leave at least $1k of debt and of collateral.
     - The liquidation bonus comes from eMode when the user is in an eMode category and the collateral belongs to it; otherwise it is the reserve bonus. The protocol fee is deducted from the bonus.
   - **Comet:** `absorb` followed by `buyCollateral` at `storeFrontPriceFactor × (1 − liquidationFactor)`.
   - **Gas:** protocol gas baseline × gas price (+20% margin) + L1 data fee. The L1 fee comes from the OP-stack `GasPriceOracle` on Base/Optimism and from `NodeInterface.gasEstimateL1Component` on Arbitrum. It is priced in USD with Chainlink ETH/USD.
5. **Alert.** If net profit ≥ `MIN_PROFIT_USD`, the opportunity is stored and the alert is queued. Redis prevents a repeat alert for the same borrower within `ALERT_DEDUP_TTL_SECONDS`.

V1 is strictly **read-only**: it loads no private keys and never sends a transaction.

## Project layout

```
src/aegis/
  config/      settings (pydantic-settings) and static chain/protocol registry
  core/        domain models, interfaces, errors, ABI helpers, DI container
  chains/      EvmChainProvider + OP-stack / Arbitrum L1 data fee providers
  protocols/   Aave v3 and Compound v3 adapters (BaseLendingProtocol)
  scanner/     event indexer, health-factor scanner, orchestrator
  estimator/   Chainlink native price oracle, gas estimator, profit estimator
  notifier/    Telegram notifier and HTML templates
  workers/     Celery app, tasks, Redis tools, healthchecks
  db/          SQLAlchemy models, repositories, session factory
migrations/    Alembic
tests/         unit (default) and integration (RUN_INTEGRATION=1)
```

## Local quickstart

Requirements: Docker with Compose v2. For development, also [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env      # fill TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ALCHEMY_API_KEY, POSTGRES_PASSWORD
make up-local             # build and start the stack
make ps-local             # all services should become "healthy"
make logs-local           # follow logs
make down-local
```

The services start in a strict order:
1. `postgres` and `redis` become healthy.
2. `migrate` runs `alembic upgrade head` and exits.
3. `scanner` and `notifier-worker` start and pass `celery inspect ping`.
4. `watcher` and `celery-beat` start. Each is reported healthy once it writes a heartbeat.

When the scanner is ready, it posts **🟢 Aegis scanner online** to Telegram. Every `STATUS_REPORT_INTERVAL_HOURS` it also posts a **📊 Aegis status** report. The report shows tracked borrowers and their debt, positions close to liquidation, indexer progress, and the opportunities and alerts of the period, so you can tell the bot is alive even when no liquidations occur.

On the first run the indexer backfills `INDEXER_LOOKBACK_DAYS` of events. This takes from a few minutes to about an hour, depending on the RPC plan. Progress shows up in the `index_synced` log lines.

## Development

```bash
make install       # uv sync --all-groups
make check         # ruff format check + ruff lint + mypy --strict + pytest
make test ARGS="tests/unit/test_aave_v3.py -k close_factor"

# Integration tests: repositories + an anvil fork of Optimism
# (Foundry: `curl -L https://foundry.paradigm.xyz | bash && foundryup`).
# DB tests use an isolated `<POSTGRES_DB>_test` database on 127.0.0.1:5433 (`make up-local`),
# never the bot's own data; they are skipped if PostgreSQL is unreachable.
make test-integration
FORK_URL_OPTIMISM=https://opt-mainnet.g.alchemy.com/v2/<key> make test-integration ARGS=-k anvil
```

Adding a chain or a lending protocol never touches the orchestrator:
- **Chain:** add a `ChainConfig` to `aegis/config/chains.py`. If it needs a custom L1-fee provider, register one in `aegis/chains/__init__.py`.
- **Protocol:** implement `BaseLendingProtocol`, then register a factory with `@PROTOCOLS.register("name")`.

## Configuration reference

All settings come from environment variables or `.env`. See [`.env.example`](.env.example) for the full list.

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — (required) | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | — (required) | Target chat / group / channel id |
| `ALCHEMY_API_KEY` | — | One key for Base, Arbitrum and OP Mainnet |
| `BASE_RPC_URLS`, `ARBITRUM_RPC_URLS`, `OPTIMISM_RPC_URLS` | empty | Extra comma-separated RPC URLs. They are tried before Alchemy and act as fallbacks |
| `ENABLED_CHAINS` | `base,arbitrum,optimism` | Chains to monitor |
| `MIN_PROFIT_USD` | `5` | Minimum net profit (after gas) to alert |
| `WATCHLIST_HF_THRESHOLD` | `1.05` | HF below which the watcher re-checks a borrower on every block |
| `MIN_DEBT_USD` | `10` | Dust filter |
| `INDEX_INTERVAL_SECONDS` / `SCAN_INTERVAL_SECONDS` | `60` / `30` | Indexer and full-scan schedules |
| `WATCHER_MIN_INTERVAL_MS` | `1000` | Minimum time between watcher ticks. The effective tick is `max(block time, this)` |
| `WATCHER_HEAD_POLL_MS` | `200` | Head polling period while the watcher waits for the next block |
| `WATCHER_PRICE_MOVE_BPS` / `WATCHER_RESCAN_COOLDOWN_SECONDS` | `50` / `5` | Oracle price move that triggers an immediate full rescan, and the cooldown between such rescans |
| `INDEXER_LOOKBACK_DAYS` | `30` | First-run backfill window |
| `INDEXER_LOG_CHUNK_BLOCKS` | `2000` | Initial `eth_getLogs` range. It shrinks automatically when the RPC rejects a request |
| `ALERT_DEDUP_TTL_SECONDS` | `3600` | Suppress repeat alerts for the same borrower |
| `STATUS_REPORT_INTERVAL_HOURS` | `1` | Periodic 📊 status report to Telegram (`0` disables it) |
| `RPC_TIMEOUT_SECONDS` / `RPC_MAX_RETRIES` / `RPC_MAX_CONCURRENCY` | `15` / `3` / `8` | RPC resilience |
| `POSTGRES_*`, `REDIS_URL` | local defaults | Overridden by docker-compose for containers |

## Security notes

- Secrets are `SecretStr` values. RPC URLs and bot tokens are redacted from all logs and errors.
- `.env` is git-ignored. `.env.example` holds placeholders only.
- Containers run as a non-root user. Postgres is published only on `127.0.0.1:5433`.
- Least privilege in compose: only `scanner` and `notifier-worker` receive the full `.env`. `postgres`, `migrate` and `celery-beat` get only the variables they need (`DatabaseSettings` / `SchedulerSettings`).
