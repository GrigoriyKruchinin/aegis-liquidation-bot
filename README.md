<div align="center">

<img src="docs/assets/aegis-logo.jpg" alt="Aegis Liquidations logo" width="180">

# Aegis Liquidations

**Liquidation analytics for lending markets on EVM Layer-2 networks.**

Aegis tracks every borrower and every liquidation on Aave v3 and Compound v3 across Base,
Arbitrum and Optimism. It values each liquidation at its block, measures how fast liquidators
react, flags liquidatable positions in real time, and reports to Telegram.

![Python](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)
![mypy](https://img.shields.io/badge/mypy-strict-2A6DB2)
![ruff](https://img.shields.io/badge/lint-ruff-D7FF64)
![Docker](https://img.shields.io/badge/docker-compose-2496ED?logo=docker&logoColor=white)
![Read-only](https://img.shields.io/badge/mode-read--only-success)

</div>

---

## Background

Lending protocols such as **Aave** and **Compound** let users borrow against collateral. Every
position has a **health factor (HF)**: the risk-adjusted value of the collateral divided by the
debt. When prices move and the HF drops **below 1**, anyone may *liquidate* the position: repay
part of the debt and receive the borrower's collateral at a discount, the **liquidation bonus**
(typically 1–10%).

Liquidations are a competitive, mostly invisible market. Aegis makes it measurable:

- **How big is it?** Every liquidation is valued at the oracle prices of its own block:
  debt repaid, collateral seized, gas (including the L1 data fee) and the liquidator's net profit.
- **Who wins it?** Liquidator contracts are ranked by activity and profit, together with their
  transaction position in the block and the number of sender addresses they use.
- **How fast is it?** For each liquidation, a binary search over archive state finds the block
  in which the position became liquidatable. A lag of 0 means it was liquidated in that very block.
- **What is at risk right now?** Positions close to liquidation are re-checked every block, and a
  Telegram alert fires as soon as one becomes profitably liquidatable.

Aegis is strictly read-only: it holds no private keys and never sends a transaction.

## Features

| | |
|---|---|
| 🌐 **Multi-chain** | Base, Arbitrum One and Optimism; one async provider per chain with failover, retries and a return to the primary endpoint |
| 🏦 **Protocols** | Aave v3 (eMode, v3.3 close factor and dust rules) and Compound v3 / Comet (`absorb` + `buyCollateral`) |
| 🏁 **Liquidation index** | Every `LiquidationCall`, `AbsorbDebt`, `AbsorbCollateral` and `BuyCollateral` event, backfilled for 30 days and then followed live |
| 💵 **Historical valuation** | USD values at the event's block, gas split per action inside multi-liquidation transactions, L1 data fee included |
| ⏱️ **Liquidator speed** | Archive binary search for the block a position became liquidatable; partial-liquidation series and dust are excluded from speed statistics |
| ⚡ **Real-time detector** | Block-synchronous watcher: re-checks near-liquidation positions every block and rescans a market on any oracle move ≥ 0.5% |
| 💰 **Accurate estimates** | Protocol liquidation terms minus gas; money math uses `int` base units and `Decimal`, never `float` |
| 📨 **Telegram** | Liquidatable position alerts, 🐋 large liquidation alerts, a 📈 daily market digest and a 📊 hourly status report |
| 📑 **Reports** | `make market-report` → Markdown, JSON and CSV exports |
| 🚦 **RPC budget** | Redis token bucket per provider key with separate shares for the watcher and background work; `eth_getLogs` routed to wide-range archive endpoints |
| 🐳 **Dockerized** | Postgres, Redis, migrations, four Celery workers, the watcher and beat, with healthchecks and a strict start order |
| 🧱 **Clean architecture** | SOLID with strategy-pattern adapters; fully typed (`mypy --strict`) and tested |

## Supported markets

| Network | Aave v3 | Compound v3 (Comet) |
|---|---|---|
| **Base** | ✅ | cUSDCv3, cWETHv3 |
| **Arbitrum One** | ✅ | cUSDCv3, cUSDC.ev3, cUSDTv3, cWETHv3 |
| **Optimism** | ✅ | cUSDCv3, cUSDTv3, cWETHv3 |

Contract addresses and their sources are documented in
[`src/aegis/config/chains.py`](src/aegis/config/chains.py). Each one was verified on-chain.

## Sample findings

From a 31-day run (18 Sep – 19 Oct 2026; 8,884 indexed events, all paid liquidations enriched):

| Market | Liquidations | Debt repaid | Liquidators' net |
|---|---|---|---|
| Arbitrum · Aave v3 | 242 | $541.4K | $33,048 |
| Base · Aave v3 | 123 | $260.9K | $11,903 |
| Optimism · Aave v3 | 387 | $266 | −$18 |
| Compound v3 (all 9 markets) | 3,562 absorbs · 964 buys | $105 | $4 |

- **Concentrated in time and size.** 75 liquidations earned ≥ $5 net ($44.9K in total). The three
  largest made up 70% of it, and most of the volume fell on four or five volatile days.
- **Same-block execution.** Every profitable liquidation with a measured lag (74 of 74) happened
  in the block in which the position became liquidatable, typically at transaction index 1–6 on
  Arbitrum. Liquidators react to the oracle update itself, not to the next block.
- **A concentrated market.** A handful of contracts take most of the profit; the leading ones
  operate on several networks at once from 10–12 sender addresses.
- **Dust elsewhere.** Optimism Aave and all Comet markets saw thousands of tiny cleanup
  liquidations with near-zero or negative net profit.

## What you get in Telegram

<table>
<tr>
<td width="50%" valign="top">

**📈 Daily market digest**
```
📈 Liquidation market · last 24h
2026-10-15 09:00 → 2026-10-16 09:00 UTC
━━━━━━━━━━━━━━━━━━
🌐 Arbitrum One: 3 liquidations
   · volume $37.9K · liquidators net $2,264.51
🌐 Optimism: 11 liquidations
   · volume $35 · liquidators net $1.31
━━━━━━━━━━━━━━━━━━
🎯 Net ≥ $5.00: 3 · $2,264.51 in total
⚡ Same-block liquidations: 3/3 (100%)
━━━━━━━━━━━━━━━━━━
🏆 Largest liquidations
1. $1,146.53 · Arbitrum One · Aave v3 · USDC/WBTC
2. $653.34 · Arbitrum One · Aave v3 · WETH/USDC
3. $464.63 · Arbitrum One · Aave v3 · USD₮0/ARB
━━━━━━━━━━━━━━━━━━
🥇 Top liquidators
1. 0x5AC9…95bd · 1 liquidation · $1,146.53
2. 0x3E7A…d288 · 1 liquidation · $653.34
3. 0x9355…5fdb · 1 liquidation · $464.63
```

</td>
<td width="50%" valign="top">

**🐋 Large liquidation**
```
🐋 Large liquidation
━━━━━━━━━━━━━━━━━━
🌐 Network: Arbitrum One
🏦 Market: Aave v3 · USDC/WBTC
🤖 Liquidator: 0x5AC9…95bd
━━━━━━━━━━━━━━━━━━
💳 Debt repaid: $18,201.35
⛽ Gas: $0.1505
💰 Liquidator net: $1,146.53
⚡ Speed: same block as it became
   liquidatable · tx #1 in block
━━━━━━━━━━━━━━━━━━
🔗 Transaction · block 515068817
🕒 2026-10-15 13:12:34 UTC
```

**🚨 Liquidatable position**
```
🚨 Liquidatable Position
━━━━━━━━━━━━━━━━━━
🌐 Network: Base
🏦 Protocol: Aave v3 · Aave v3
🔴 Health Factor: 0.9712
💳 Debt to cover: 1,000 USDC (≈ $1,000.00)
💎 Collateral: WETH (seize ≈ $1,050.00)
⛽ Gas (L2 + L1 data): $0.0312
💰 Est. liquidator net profit: $44.97
```

</td>
</tr>
</table>

The **📊 hourly status report** lists tracked borrowers and debt per market, positions close to
liquidation, indexer progress, and the period's alerts and market liquidations, so a quiet chat
still shows the system is alive.

## Architecture

```
                 ┌──────────────┐
                 │ celery-beat  │ schedules everything below
                 └──────┬───────┘
      ┌─────────────────┼──────────────────────┬──────────────────────┐
      ▼                 ▼                      ▼                      ▼
┌───────────┐   ┌───────────────┐   ┌──────────────────┐   ┌──────────────────┐
│ scanner   │   │ market-worker │   │ enrich-worker    │   │ notifier-worker  │
│ -Q scan   │   │ -Q market     │   │ -Q enrich        │   │ -Q notify        │
│ borrowers │   │ liquidation   │   │ receipt, USD at  │   │ alerts, digest,  │
│ + HF scan │   │ events index  │   │ block, gas, lag  │   │ status → Telegram│
└─────┬─────┘   └───────┬───────┘   └────────┬─────────┘   └────────▲─────────┘
      │                 │                    │ 🐋 large liquidation  │
      │   ┌─────────────┴────────┐           └───────────────────────┤
      │   │ watcher (1 loop/chain)│ every block: oracle prices +      │
      │   │ hot set re-check      │ near-liquidation positions ───────┘
      │   └─────────────┬────────┘
      ▼                 ▼
 Multicall3 eth_call · eth_getLogs (log-only archive endpoints) · receipts
      │
      ▼
 ┌──────────┐  borrowers · checkpoints · opportunities · liquidation_events
 │ Postgres │  sightings · uptime_minutes · notifications
 └──────────┘
```

1. **Borrower discovery.** `Borrow` (Aave) and base `Withdraw` (Comet) events are indexed in
   adaptive `eth_getLogs` chunks. The chunk size shrinks to what each endpoint accepts and is not
   grown back into a rejected range. Checkpoints stay `reorg_safety_blocks` behind the head.
2. **Health scan.** `getUserAccountData` (Aave) and `isLiquidatable` + balances (Comet) are
   batched through Multicall3; a full scan of all active borrowers runs every
   `SCAN_INTERVAL_SECONDS`.
3. **Real-time watcher.** One asyncio loop per chain, synchronised with block production. On each
   new block it reads every oracle price of the chain in **one** multicall (a move ≥
   `WATCHER_PRICE_MOVE_BPS` triggers an immediate rescan) and re-checks borrowers with HF below
   `WATCHLIST_HF_THRESHOLD`. It records which minutes it was online, so coverage statistics can
   tell "not detected" from "offline".
4. **Estimate and alert.**
   - **Aave v3 (v3.3 rules):** close factor 50% of the *total* debt, 100% of the reserve debt when
     HF < 0.95 or a leg is below $2k; partial liquidations must leave at least $1k; eMode or
     reserve bonus minus the protocol fee.
   - **Comet:** `absorb` followed by `buyCollateral` at
     `storeFrontPriceFactor × (1 − liquidationFactor)`.
   - **Gas:** protocol gas baseline × gas price (+20% margin) + L1 data fee (OP-stack
     `GasPriceOracle`, Arbitrum `NodeInterface`), priced with Chainlink ETH/USD under a per-chain
     staleness limit derived from the feed heartbeat.

   Positions with net profit ≥ `MIN_PROFIT_USD` are stored and alerted once per
   `ALERT_DEDUP_TTL_SECONDS`.
5. **Liquidation analytics.** A separate checkpointed stream indexes liquidation events. The
   enrichment worker adds the receipt (sender, tx index, gas, L1 fee), USD values at the event's
   block, the per-action gas share, the liquidation lag, follow-up detection, and whether the
   detector had seen the position first. Paid liquidations are enriched before Comet bookkeeping
   events, and transient RPC errors are retried while permanent gaps are recorded in `data_gaps`.

## Quickstart

Requirements: Docker with Compose v2, an [Alchemy](https://dashboard.alchemy.com) key with Base,
Arbitrum and OP Mainnet enabled, and a Telegram bot from [@BotFather](https://t.me/BotFather).
For development you also need [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env      # fill TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ALCHEMY_API_KEY, POSTGRES_PASSWORD
make up                   # build and start the stack
make ps                   # all services should become "healthy"
make logs                 # follow logs
make market-report DAYS=14
make down
```

Start order:
1. `postgres` and `redis` become healthy.
2. `migrate` runs `alembic upgrade head` and exits.
3. `scanner`, `market-worker`, `enrich-worker` and `notifier-worker` pass `celery inspect ping`.
4. `watcher` and `celery-beat` start and are healthy once they write a heartbeat.

On the first run the indexers backfill 30 days of events. With the default public log endpoints
this takes several hours; progress shows up in the `index_synced` log lines and in the status
report.

## Market report

`make market-report DAYS=N` writes `data/market/report.md`, `report.json`, `liquidations.csv`
and `sightings.csv`:

- detector uptime per chain;
- liquidations, debt repaid, gross and net profit per market, and the profit distribution;
- the most active and the most profitable liquidator contracts (share, median lag, median tx
  index, gas, chains, sender addresses);
- liquidation speed buckets (same block, 1 block, 2–5, …) for all and for profitable liquidations;
- detector coverage: known borrowers, positions detected before the liquidation, lead time;
- the profit pool (Aave, net ≥ $5) projected to 30 days.

The report reads only the database; no RPC or secrets are needed.

## Project layout

```
src/aegis/
  config/      settings (pydantic-settings) and the static chain / protocol registry
  core/        domain models, interfaces, errors, ABI helpers, DI container
  chains/      EvmChainProvider, OP-stack / Arbitrum L1-fee providers, RPC cost accounting
  protocols/   Aave v3 and Compound v3 adapters (BaseLendingProtocol)
  scanner/     event indexers, health-factor scanner, orchestrator, block-synchronous watcher
  estimator/   Chainlink native price oracle, gas estimator, profit estimator
  market/      liquidation event stream, enrichment (valuation, gas, lag search)
  reporting/   status report and daily market digest
  notifier/    Telegram notifier and HTML templates
  workers/     Celery app and tasks, watcher service, Redis tools, healthchecks
  db/          SQLAlchemy models, repositories, session factory
  tools/       CLIs (market_report, rpc_latency)
migrations/    Alembic
docs/assets/   images used by the documentation
tests/         unit (default) and integration (RUN_INTEGRATION=1)
```

## Development

```bash
make install       # uv sync --all-groups
make check         # ruff format check + ruff lint + mypy --strict + pytest
make test ARGS="tests/unit/test_aave_v3.py -k close_factor"

# Integration tests: repositories against PostgreSQL + an anvil fork of Optimism
# (Foundry: `curl -L https://foundry.paradigm.xyz | bash && foundryup`).
# DB tests use an isolated `<POSTGRES_DB>_test` database on 127.0.0.1:5433 (`make up`)
# and are skipped if PostgreSQL is unreachable.
make test-integration
FORK_URL_OPTIMISM=https://opt-mainnet.g.alchemy.com/v2/<key> make test-integration ARGS=-k anvil
```

Adding a chain or a lending protocol never touches the orchestrator:
- **Chain:** add a `ChainConfig` to `aegis/config/chains.py`. If it needs a custom L1-fee
  provider, register one in `aegis/chains/__init__.py`.
- **Protocol:** implement `BaseLendingProtocol` (including the liquidation-event hooks), then
  register a factory with `@PROTOCOLS.register("name")`.

## Configuration reference

All settings come from environment variables or `.env`. See [`.env.example`](.env.example) for
the full list.

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — (required) | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | — (required) | Target chat / group / channel id |
| `ALCHEMY_API_KEY` | — | One key for Base, Arbitrum and OP Mainnet |
| `BASE_RPC_URLS`, `ARBITRUM_RPC_URLS`, `OPTIMISM_RPC_URLS` | empty | Extra comma-separated RPC URLs, tried before Alchemy, acting as fallbacks |
| `BASE_LOG_RPC_URLS`, `ARBITRUM_LOG_RPC_URLS`, `OPTIMISM_LOG_RPC_URLS` | public archive nodes | Endpoints used only for `eth_getLogs`, tried before the main ones (Alchemy's free tier caps `eth_getLogs` at 10 blocks). Defaults: Tenderly's public Base gateway, `arb1.arbitrum.io`, `mainnet.optimism.io`. An empty value disables them |
| `RPC_CU_PER_SECOND` | `300` | Throughput budget per provider key in compute units/s, across all chains and services (Redis token bucket, prevents HTTP 429). Set it to about 80% of your plan's CUPS and within the monthly quota; `0` disables it |
| `RPC_RESERVE_PCT` | `30` | Share of the budget dedicated to the watcher; background work gets the rest. Separate buckets, so neither side can starve the other |
| `RPC_LOG_CU_PER_SECOND` | `150` | Budget per log-only endpoint |
| `RPC_TIMEOUT_SECONDS` / `RPC_MAX_RETRIES` / `RPC_MAX_CONCURRENCY` | `15` / `3` / `8` | RPC resilience |
| `ENABLED_CHAINS` | `base,arbitrum,optimism` | Chains to monitor |
| `MIN_PROFIT_USD` | `5` | Net profit (after gas) from which a liquidatable position is alerted; also the digest's "meaningful" threshold |
| `WATCHLIST_HF_THRESHOLD` | `1.05` | HF below which the watcher re-checks a borrower on every block |
| `MIN_DEBT_USD` | `10` | Dust filter |
| `INDEX_INTERVAL_SECONDS` / `SCAN_INTERVAL_SECONDS` | `60` / `30` | Borrower indexer and full-scan schedules |
| `INDEXER_LOOKBACK_DAYS` / `INDEXER_LOG_CHUNK_BLOCKS` | `30` / `10000` | Borrower backfill window and initial `eth_getLogs` range |
| `WATCHER_MIN_INTERVAL_MS` / `WATCHER_HEAD_POLL_MS` | `1000` / `200` | Minimum time between watcher ticks; head polling period |
| `WATCHER_PRICE_MOVE_BPS` / `WATCHER_RESCAN_COOLDOWN_SECONDS` | `50` / `5` | Oracle move that triggers a full rescan; cooldown between rescans |
| `ALERT_DEDUP_TTL_SECONDS` | `3600` | Suppress repeat alerts for the same position |
| `STATUS_REPORT_INTERVAL_HOURS` | `1` | 📊 status report period (`0` disables it) |
| `MARKET_ENABLED` / `MARKET_LOOKBACK_DAYS` | `true` / `30` | Liquidation analytics and its backfill window |
| `MARKET_INDEX_INTERVAL_SECONDS` / `MARKET_ENRICH_INTERVAL_SECONDS` | `300` / `120` | How often liquidations are indexed and enriched |
| `MARKET_LAG_WINDOW_MINUTES` / `MARKET_LAG_MIN_GROSS_USD` | `120` / `1` | Archive search window for the liquidation lag; dust below this gross profit skips the search |
| `MARKET_DIGEST_HOUR_UTC` | `9` | Hour of the 📈 daily digest (`-1` disables it) |
| `MARKET_ALERT_MIN_NET_USD` / `MARKET_ALERT_MAX_AGE_MINUTES` | `500` / `60` | 🐋 alert threshold (`0` disables it); older (backfilled) liquidations never alert |
| `POSTGRES_*`, `REDIS_URL` | local defaults | Overridden by docker-compose for containers |

## Security

- Read-only by design: no private keys, no signer, no transaction sending.
- Secrets are `SecretStr` values; RPC URLs and bot tokens are redacted from all logs and errors.
- `.env` is git-ignored; `.env.example` holds placeholders only.
- Containers run as a non-root user; Postgres is published only on `127.0.0.1:5433`.
- Least privilege in compose: only the application services receive the full `.env`;
  `postgres`, `migrate` and `celery-beat` get only the variables they need.
- Every dynamic value in Telegram messages is HTML-escaped; alerts are idempotent.

## Disclaimer

Aegis is an analytics tool. Its figures are estimates derived from on-chain data and oracle
prices; nothing in it is financial advice.
