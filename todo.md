# Aegis Liquidations — Roadmap & Task Checklist

Legend: `[ ]` todo · `[x]` done · `[~]` in progress

---

## Phase 0 — Infrastructure Setup

- [x] Ask clarifying questions (networks, protocols, profit threshold, admin credentials)
  - Networks: Base, Arbitrum, Optimism
  - Protocols: Aave v3, Compound v3 (Comet)
  - `MIN_PROFIT_USD=5`
  - Admin credentials via `.env` (`ADMIN_USERNAME`, `ADMIN_PASSWORD_HASH`)
- [x] Create `CLAUDE.md` (commands, style, workflow, safety invariants)
- [x] Create `.claude/agents/implementation-agent.md`
- [x] Create `.claude/agents/security-critic-agent.md`
- [x] Create skills: `check-code-quality`, `run-tests`, `audit-security`
- [x] Create `.claude/settings.json` (dev command allowlist, `.env` read deny)
- [x] Create `todo.md`
- [x] Await user command **"Start Implementation"**

---

## Stage 1 — Version 1: Local Opportunity Detector & Telegram Alerts (Read-Only)

### 1.0 Prerequisites
- [x] Give the user a Russian guide: create bot via `@BotFather`, get `TELEGRAM_BOT_TOKEN`, get `TELEGRAM_CHAT_ID`, obtain RPC URLs (Alchemy/Infura/QuickNode), fill local `.env` (no VPS steps)

### 1.1 Project scaffolding
- [x] `pyproject.toml` with `uv` (Python 3.12), dependency groups `dev`, `test`
- [x] Ruff config (line length 100, rule set incl. `B`, `UP`, `S`, `ASYNC`, `ANN`, `I`)
- [x] Mypy strict config (+ pydantic plugin)
- [x] Pytest config (`asyncio_mode=auto`, `integration` marker)
- [x] Root `Makefile` (install, format, format-check, lint, typecheck, test, check, migrate, up-local, down-local, logs-local, ps-local)
- [x] `src/aegis/` package skeleton per `CLAUDE.md` layout
- [x] `.env.example` with all keys and placeholders (single `ALCHEMY_API_KEY` for all chains + optional per-chain fallback URLs)
- [ ] Pre-commit hooks (ruff, mypy) — optional

### 1.2 Configuration
- [x] `config/settings.py` — `AppSettings` (pydantic-settings): env, log level, DB, Redis, Telegram token/chat id (`SecretStr`), `MIN_PROFIT_USD`, intervals, RPC resilience
- [x] `config/chains.py` — per-chain config (chain id, provider kind, block time, native feed, explorer, Alchemy network, reorg margin)
- [x] Static protocol deployment registry (Aave v3 `PoolAddressesProvider` → Pool/Oracle resolved at runtime; 9 Comet markets) — verified on mainnet
- [x] Settings validation tests

### 1.3 Core domain & interfaces
- [x] Domain models: `Asset`, `MarketRef`, `HealthCheck`, `Position`, `CollateralLeg`, `DebtLeg`, `LiquidationQuote`, `GasEstimate`, `ProfitEstimate`, `LiquidationOpportunity`, `OpportunitySummary`
- [x] Interfaces: `BaseChainProvider`, `BaseLendingProtocol`, `IHealthFactorScanner`, `IBorrowerIndexer`, `IPriceOracle`, `IGasEstimator`, `IProfitEstimator`, `INotifier`, `INotificationDispatcher`, `IAlertDeduplicator`, repository interfaces
- [x] `core/errors.py` exception hierarchy
- [x] DI container (`core/container.py`) + chain & protocol registries (`core/registry.py`)
- [x] Structured logging setup (structlog JSON) + secret redaction (`core/redact.py`)

### 1.4 Chain providers
- [x] Generic `EvmChainProvider` (AsyncWeb3, retries, timeouts, semaphore, RPC fallback list)
- [x] Multicall3 `aggregate3` batch helper
- [x] `OpStackChainProvider` (Base, Optimism: `GasPriceOracle.getL1FeeUpperBound`) and `ArbitrumChainProvider` (`NodeInterface.gasEstimateL1Component`)
- [x] Unit tests with mocked RPC

### 1.5 Lending protocol adapters
- [x] Aave v3 adapter: discovery from `Borrow` events, batched `getUserAccountData`, user-config bitmap + aToken/debt-token balances, bonus & protocol fee, close factor (v3.3 rules), oracle prices
- [x] Compound v3 adapter: discovery from `Withdraw` events, batched `isLiquidatable` + balances, `storeFrontPriceFactor`/`liquidationFactor` discount, reserves vs target check, native-denominated markets
- [x] ABI definitions as typed human-readable signatures (`core/abi.py`, per-adapter constants) instead of JSON files
- [x] Unit tests with encoded fixtures

### 1.6 Borrower indexer & scanner
- [x] Incremental event indexer (checkpoint per market in DB, adaptive chunked `eth_getLogs`, reorg safety margin, bounded blocks per run)
- [x] `HealthFactorScanner`: batch HF checks, filter `HF < 1.0` above dust, emit opportunities
- [x] Watchlist of near-liquidation positions (HF < `WATCHLIST_HF_THRESHOLD`) — re-checked on **every block** by the `watcher` service
- [x] Fast `watcher` service (asyncio, one loop per chain, block phase-locked head polling): per-block hot-set checks + chain-wide oracle price snapshot in one multicall; price move ≥ `WATCHER_PRICE_MOVE_BPS` triggers an immediate background full rescan
- [x] `make rpc-latency` — compare configured RPC endpoints (round trip + head freshness)
- [x] Hourly 📊 status report to Telegram (`STATUS_REPORT_INTERVAL_HOURS`)
- [x] Orchestrator iterating registered markets per chain (no protocol-specific code)

### 1.7 Profit estimator
- [x] Best debt/collateral pair selection (top quotes by gross, then best net)
- [x] Gross profit = liquidation bonus value in USD (self-funded assumption)
- [x] Gas estimate: protocol gas baseline × gas price (+20% margin) + L1 data fee → USD via Chainlink ETH/USD (staleness-checked)
- [x] Net profit = gross − gas; `Decimal` throughout
- [x] Unit tests with hand-computed scenarios

### 1.8 Database (PostgreSQL)
- [x] Async engine & session factory
- [x] Models: `borrowers` (incl. latest HF snapshot), `opportunities`, `notifications`, `indexer_checkpoints` (chains/protocols stay in the static registry)
- [x] Repositories implementing interfaces (+ integration tests on real Postgres)
- [x] Alembic setup + initial migration (`0001`)

### 1.9 Redis & Celery
- [x] Celery app (Redis broker/backend), JSON serializer, task routes (`scan`, `notify` queues)
- [x] Beat schedule: indexer sync, full scan, watchlist scan per chain; Redis locks prevent overlapping runs
- [x] `notify_opportunity` task with retries + backoff (honours Telegram `retry_after`)
- [x] Redis-based dedup (chain+protocol+market+borrower key with TTL), released on delivery failure

### 1.10 Telegram notifier
- [x] `TelegramNotifier(INotifier)` via `httpx` (retry on 429 / 5xx, token never logged)
- [x] HTML template "🚨 Liquidation Opportunity": network, protocol/market, borrower (`<code>` + explorer link), HF, debt, collateral, bonus %, gas cost, net profit, blockquote summary, separators
- [x] Escape all dynamic values; template tests
- [x] Startup "🟢 Aegis scanner online" message

### 1.11 Docker (local)
- [x] `Dockerfile` (multi-stage, `uv`, non-root user)
- [x] `docker-compose.local.yml`: `postgres`, `redis`, `migrate` (one-shot), `scanner` (Celery `scan` queue), `notifier-worker` (Celery `notify` queue), `celery-beat`
- [x] Healthchecks: Postgres (`pg_isready`), Redis (`redis-cli ping`), workers (`celery inspect ping`), beat (scan heartbeat in Redis)
- [x] `depends_on` with `condition: service_healthy` / `service_completed_successfully`

### 1.12 Testing & docs
- [x] Integration test on anvil fork (`tests/integration/test_anvil_fork.py`): real Optimism borrower made underwater via a mock AaveOracle source, opportunity detected (incl. L1 fee) and notification rendered
- [x] `README.md`: overview, architecture diagram, local quickstart, configuration reference
- [x] `/audit-security` pass on V1 — no CRITICAL; fixed: HIGH-1 Aave eMode bonus/LT, MEDIUM-2 v3.3 close factor vs total debt, MEDIUM-3 indexer shrinks only on range errors (+HTTP 400/413), MEDIUM-4 URL userinfo redaction, MEDIUM-5 gas price / L1 fee caching + skip sub-threshold quotes, LOW-6 least-privilege env in compose (scoped `DatabaseSettings` / `SchedulerSettings`), LOW-9 atomic Lua lock release, LOW-10 Telegram 429 exhaustion + idempotent sends, LOW-12 Aave v3.3 min-leftover dust rule, LOW-13 startup message without a full container
- [ ] Deferred V1 audit findings → tracked in **2.7** below (must be done before live execution)

### 1.13 Market measurement (data for the V2 go/no-go decision)
- [x] Index `LiquidationCall` (Aave) and `AbsorbDebt` / `AbsorbCollateral` / `BuyCollateral` (Comet): a separate checkpointed `liquidations` stream (30-day backfill, then follow)
- [x] Enrich each event: tx sender and liquidator contract, gas + L1 fee, tx index, USD value at the block's oracle prices, gross/net profit
- [x] Lag: binary search over archive state for the block a position became liquidatable → blocks/seconds until a competitor liquidated it (0 = same-block back-run)
- [x] Our sightings of liquidatable positions (any size) + watcher uptime per minute → seen first? lead time? offline vs missed?
- [x] `make market-report DAYS=N` → Markdown / JSON / CSV in `data/market/`; hourly Telegram status shows market liquidations
- [ ] After 2–4 weeks of running locally: analyse `data/market/` (competitor share & speed, our lead, addressable pool) → V2 go/no-go

---

## Stage 2 — Version 2: On-Chain Execution, Smart Contracts & CI/CD

### 2.0 Prerequisites
- [ ] Give the user a Russian guide: VPS provisioning, SSH keys & hardening, Docker Engine install, deploy keys for GitHub, hot wallet creation & gas funding per chain
- [ ] **User step: upgrade the RPC plan before going live.** The Alchemy free tier (30M CU/month, 300 CU/s) only sustains ~11 CU/s on average, while a 24/7 per-block bot needs ~150–250 CU/s (~400–650M CU/month). Options: Alchemy Pay-As-You-Go, a flat-rate provider (QuickNode / Chainstack / dRPC), or self-hosted nodes. Then set `RPC_CU_PER_SECOND` to ~80% of the new throughput limit (and within the monthly quota)
- [ ] Go/no-go economics review: compare monthly costs (RPC + VPS + gas incl. lost races) with the opportunity pool measured by V1 (see 1.13)

### 2.1 Smart contracts (Foundry)
- [ ] `contracts/` Foundry project, remappings, OpenZeppelin dependency
- [ ] `AegisLiquidator.sol`: owner-only, self-funded liquidation path (Aave v3 `liquidationCall`, Compound v3 `absorb` + `buyCollateral`)
- [ ] Flash-loan liquidation path with callback validation
- [ ] Collateral → debt swap via pluggable DEX router (Uniswap v3 / Aerodrome / Velodrome) with `minAmountOut`
- [ ] Profit sweep to owner, `rescue` function, reentrancy guards
- [ ] Foundry fork tests per chain; gas snapshots
- [ ] Slither + manual audit; deploy scripts (`forge script`) per chain

### 2.2 Flash loan adapters
- [ ] `IFlashLoanProvider` (fee quote, max available liquidity, calldata builder)
- [ ] Aave v3 flash loan adapter (premium from `FLASHLOAN_PREMIUM_TOTAL`)
- [ ] Balancer Vault adapter (0% fee)
- [ ] Equalizer adapter
- [ ] Provider selection: cheapest provider with enough liquidity

### 2.3 Dynamic Liquidity Router
- [ ] `ALLOW_FLASH_LOANS: bool` hard toggle in settings
- [ ] Decision: hot wallet balance ≥ required debt → self-fund (0% fee)
- [ ] Else, if `ALLOW_FLASH_LOANS` and net profit after flash fee > threshold → flash loan
- [ ] Else skip and persist/log skip reason
- [ ] Exhaustive unit tests of the decision matrix

### 2.4 Execution engine
- [ ] Hot wallet signer (key from `SecretStr`, optionally file/KMS), never logged
- [ ] Nonce manager (per chain, Redis lock)
- [ ] Gas strategy (EIP-1559, priority fee caps, max gas USD guard)
- [ ] Pre-send simulation (`eth_call`) and profit re-check at latest block
- [ ] Private / sequencer-direct RPC submission option
- [ ] Receipt tracking, stuck tx replacement, result persistence (`executions` table)
- [ ] Circuit breaker (pause after N consecutive failures)

### 2.5 Production infra & CI/CD
- [ ] `docker-compose.yml` (prod): resource limits, restart policies, log rotation, healthchecks, secrets via env file
- [ ] GitHub Actions CI: ruff, mypy, pytest, forge test, docker build
- [ ] GitHub Actions CD: build & push image to GHCR, SSH deploy to VPS, migrations, health verification
- [ ] Backups for Postgres

### 2.6 Advanced Telegram notifications
- [ ] 🟢 Successful deal: tx hash (explorer link), network, gross profit, gas spent, flash loan fee (if used), net profit
- [ ] 🔴 Failed / reverted execution alert with reason
- [ ] ⚠️ Gas alert: hot wallet native balance below per-chain threshold
- [ ] 📊 Daily digest (Celery beat): deals, win/loss, gross/net, gas, per-chain breakdown
- [ ] 📊 Monthly summary on the 1st of every month
- [ ] `/audit-security` pass on V2

### 2.8 Competitive latency (win liquidations, not just detect them)
- [ ] Measure end-to-end: block produced → opportunity detected → tx included; track win rate vs competing liquidators (who liquidated our misses, in which block)
- [ ] Freshest data source per chain: dedicated/self-hosted nodes in the sequencer's region; WebSocket `newHeads` instead of polling
- [ ] Base / OP-stack: consume **Flashblocks** (~200 ms preconfirmations) to see state before the 2 s block is sealed
- [ ] Arbitrum: evaluate **Timeboost** express lane vs plain FCFS ordering; minimise latency to the sequencer endpoint
- [ ] Predictive engine: per position, precompute the oracle price at which HF < 1; react to oracle update transactions / events immediately instead of waiting for the next scan
- [ ] Pre-built calldata + pre-signed tx templates per hot position (only amount/nonce/gas updated at send time); direct submission to the sequencer
- [ ] Gas/priority-fee bidding strategy bounded by expected profit
- [ ] Event-driven cache of borrower balances (apply `Supply`/`Borrow`/`Repay`/`Withdraw`/`Transfer` events) to avoid RPC reads on the hot path

### 2.7 Hardening carried over from the V1 audit (MUST be addressed in V2)
- [ ] **(V1 audit LOW-7) RPC failover stickiness** — `EvmChainProvider._active` never returns to the primary endpoint after a single failure (`src/aegis/chains/evm.py`, `_request`). Add periodic re-probing of endpoint #0 (e.g. after N seconds or N successful calls on a fallback) and per-endpoint health stats; prefer private/sequencer RPC for execution, Alchemy only as fallback. Unit-test the recovery path.
- [ ] **(V1 audit LOW-8) Semaphore held across retries/failover** — `_request` holds the per-chain semaphore through backoff sleeps and all endpoints, so a few slow endpoints can starve the whole chain (including watchlist scans and, in V2, time-critical execution). Acquire the semaphore per attempt only; consider separate concurrency pools for scanning vs execution so execution is never blocked by scans.
- [ ] **(V1 audit LOW-11) Native price staleness + L2 sequencer status** — `ChainlinkNativePriceOracle` accepts answers up to 26 h old (`src/aegis/estimator/oracle.py`). Make `max_age_seconds` per chain from the feed heartbeat (Base/OP ETH/USD 1200 s, Arbitrum 86400 s + margin) in `ChainConfig`; add the Chainlink L2 Sequencer Uptime Feed check (+ grace period) and refuse to price / execute while the sequencer is down. Applies to gas costing and Comet `native`-denominated markets.
- [ ] **(V1 audit LOW-14a) Required Postgres password** — `DatabaseSettings.postgres_password` defaults to `"aegis"`; make it required (no default) for `APP_ENV=production` and validate it is not a placeholder (`change-me`).
- [ ] **(V1 audit LOW-14b) Pin base images by digest** — `Dockerfile` uses `python:3.12-slim` and `ghcr.io/astral-sh/uv:0.9.8` by tag; pin `@sha256:` digests for prod builds and let Dependabot/Renovate bump them.
- [ ] **(V1 audit LOW-14c) Unbounded borrower lists** — `SqlBorrowerRepository.list_active` loads every active borrower of a market and the scanner `gather`s all check batches at once. Stream with keyset pagination (`id > last_id LIMIT n`) and bound concurrent batches, to keep memory/RPC bursts flat as the borrower set grows.

---

## Stage 3 — Version 3: FastAPI Admin Dashboard & Multi-Chain Expansion

### 3.1 FastAPI application
- [ ] App factory, DI wiring, lifespan (DB/Redis), CORS & security headers
- [ ] Health & readiness endpoints

### 3.2 Auth system
- [ ] Login with `ADMIN_USERNAME` + bcrypt `ADMIN_PASSWORD_HASH` from `.env`
- [ ] JWT access tokens (short TTL) in HttpOnly cookie; Basic Auth fallback for API
- [ ] Login rate limiting; `make hash-password` helper

### 3.3 Analytics API
- [ ] Net profit time series (day/week/month), per chain/protocol
- [ ] Gas expenditure series
- [ ] Win/loss ratio, success rate, skip reasons breakdown
- [ ] Top deals table (paginated, sortable)
- [ ] Efficient aggregate queries / materialized views

### 3.4 Dashboard UI
- [ ] Jinja2 templates + Chart.js: net profit chart, gas chart, win/loss donut, top deals table
- [ ] Live status: scanner heartbeat, wallet balances, flash-loan toggle state (read-only)
- [ ] Dashboard service in `docker-compose.yml` behind reverse proxy (Caddy/Traefik, HTTPS)

### 3.5 Multi-chain expansion plugins
- [ ] Plugin discovery via entry points for chains, protocols, DEX routers, flash loan providers
- [ ] New L2/L3 chains (e.g. Scroll, Linea, zkSync, Mantle) as plugins
- [ ] Additional DEX routers (1inch / 0x / Odos aggregators)
- [ ] Additional lending protocols (e.g. Morpho Blue, Radiant, Moonwell)
- [ ] Plugin authoring guide in `README.md`
- [ ] `/audit-security` pass on V3
