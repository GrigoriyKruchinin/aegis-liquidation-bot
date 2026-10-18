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
- [ ] Await user command **"Start Implementation"**

---

## Stage 1 — Version 1: Local Opportunity Detector & Telegram Alerts (Read-Only)

### 1.0 Prerequisites
- [ ] Give the user a Russian guide: create bot via `@BotFather`, get `TELEGRAM_BOT_TOKEN`, get `TELEGRAM_CHAT_ID`, obtain RPC URLs (Alchemy/Infura/QuickNode), fill local `.env` (no VPS steps)

### 1.1 Project scaffolding
- [ ] `pyproject.toml` with `uv` (Python 3.12), dependency groups `dev`, `test`
- [ ] Ruff config (line length 100, rule set incl. `B`, `UP`, `S`, `ASYNC`, `ANN`, `I`)
- [ ] Mypy strict config (+ pydantic plugin)
- [ ] Pytest config (`asyncio_mode=auto`, `integration` marker)
- [ ] Root `Makefile` (install, format, format-check, lint, typecheck, test, check, migrate, up-local, down-local, logs-local)
- [ ] `src/aegis/` package skeleton per `CLAUDE.md` layout
- [ ] `.env.example` with all keys and placeholders
- [ ] Pre-commit hooks (ruff, mypy) — optional

### 1.2 Configuration
- [ ] `config/settings.py` — `AppSettings` (pydantic-settings): env, log level, DB URL, Redis URL, Telegram token/chat id (`SecretStr`), `MIN_PROFIT_USD`, scan interval, RPC concurrency
- [ ] `config/chains.py` — per-chain config models (chain id, RPC URLs, Multicall3, block time, native token, enabled protocols)
- [ ] Static protocol deployment registry (Aave v3 Pool / PoolDataProvider / Oracle; Compound v3 Comet markets) for Base, Arbitrum, Optimism
- [ ] Settings validation tests

### 1.3 Core domain & interfaces
- [ ] Domain models: `Chain`, `Asset`, `Borrower`, `Position`, `CollateralLeg`, `DebtLeg`, `LiquidationOpportunity`, `ProfitEstimate`
- [ ] Interfaces: `BaseChainProvider`, `BaseLendingProtocol`, `IHealthFactorScanner`, `IBorrowerIndexer`, `IPriceOracle`, `IGasEstimator`, `IProfitEstimator`, `INotifier`, repository interfaces
- [ ] `core/errors.py` exception hierarchy
- [ ] DI container / factories (`core/container.py`) + chain & protocol registries
- [ ] Structured logging setup

### 1.4 Chain providers
- [ ] Generic `EvmChainProvider` (AsyncWeb3, retries, timeouts, semaphore, RPC fallback list)
- [ ] Multicall3 batch helper
- [ ] `BaseProvider`, `ArbitrumProvider`, `OptimismProvider` (chain-specific L1 data fee: OP-stack `GasPriceOracle`, Arbitrum `NodeInterface.gasEstimateL1Component`)
- [ ] Unit tests with mocked RPC

### 1.5 Lending protocol adapters
- [ ] Aave v3 adapter: borrower discovery from `Borrow`/`Supply`/`Repay`/`LiquidationCall` events, `getUserAccountData` batched, per-reserve user data, liquidation bonus & close factor (50% / 100% when HF < 0.95), oracle prices
- [ ] Compound v3 adapter: discover accounts from `Withdraw`/`Supply`/`AbsorbDebt` events, `isLiquidatable` batched, collateral balances, `storeFrontPriceFactor`, `liquidationFactor`, absorb + buyCollateral profit model
- [ ] ABI files under `src/aegis/protocols/abi/`
- [ ] Unit tests with recorded fixtures

### 1.6 Borrower indexer & scanner
- [ ] Incremental event indexer (checkpoint last block per chain/protocol in DB, chunked `eth_getLogs`, reorg safety margin)
- [ ] `HealthFactorScanner`: batch HF checks, filter `HF < 1.0`, emit opportunities
- [ ] Watchlist of near-liquidation positions (e.g. HF < 1.05) scanned more frequently
- [ ] Orchestrator iterating registered chains × protocols (no protocol-specific code)

### 1.7 Profit estimator
- [ ] Best debt/collateral pair selection
- [ ] Gross profit = liquidation bonus value in USD (self-funded assumption)
- [ ] Gas estimate: L2 execution gas (protocol-specific baseline) × gas price + L1 data fee → USD via native token price
- [ ] Net profit = gross − gas; `Decimal` throughout
- [ ] Unit tests with hand-computed scenarios

### 1.8 Database (PostgreSQL)
- [ ] Async engine & session factory
- [ ] Models: `chains`, `protocols`, `borrowers`, `positions` (snapshot), `opportunities`, `notifications`, `indexer_checkpoints`
- [ ] Repositories implementing interfaces
- [ ] Alembic setup + initial migration

### 1.9 Redis & Celery
- [ ] Celery app (Redis broker/backend), JSON serializer, task routes (`scan`, `notify` queues)
- [ ] Beat schedule: indexer sync, full scan, watchlist scan per chain
- [ ] `notify_opportunity` task with retries + backoff
- [ ] Redis-based dedup (borrower+chain+protocol key with TTL) to prevent alert spam

### 1.10 Telegram notifier
- [ ] `TelegramNotifier(INotifier)` via `httpx` (rate-limit aware, retry on 429)
- [ ] HTML template "🚨 Liquidation Opportunity": network, protocol, borrower (`<code>` + explorer link), HF, debt asset/size, collateral asset, bonus %, gas cost, net profit, blockquote summary, separators
- [ ] Escape all dynamic values; template snapshot tests
- [ ] Startup / health "🟢 Scanner online" message

### 1.11 Docker (local)
- [ ] `Dockerfile` (multi-stage, `uv`, non-root user)
- [ ] `docker-compose.local.yml`: `postgres`, `redis`, `migrate` (one-shot), `celery-worker`, `celery-beat`, `scanner`, `notifier-worker`
- [ ] Healthchecks for Postgres (`pg_isready`), Redis (`redis-cli ping`), Celery (`celery inspect ping`), scanner (heartbeat file / Redis key)
- [ ] `depends_on` with `condition: service_healthy` / `service_completed_successfully`

### 1.12 Testing & docs
- [ ] Integration test on anvil fork: craft an underwater Aave v3 position, assert opportunity detected and notification rendered
- [ ] `README.md`: overview, architecture diagram, local quickstart, configuration reference
- [ ] `/audit-security` pass on V1

---

## Stage 2 — Version 2: On-Chain Execution, Smart Contracts & CI/CD

### 2.0 Prerequisites
- [ ] Give the user a Russian guide: VPS provisioning, SSH keys & hardening, Docker Engine install, deploy keys for GitHub, hot wallet creation & gas funding per chain

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
