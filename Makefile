.PHONY: install format format-check lint typecheck test test-integration check migrate up down logs ps rpc-latency market-report

COMPOSE = docker compose

install:
	uv sync --all-groups

format:
	uv run ruff format .
	uv run ruff check --fix .

format-check:
	uv run ruff format --check .

lint:
	uv run ruff check .

typecheck:
	uv run mypy src tests

test:
	uv run pytest $(ARGS)

# Needs anvil (Foundry) and PostgreSQL on 127.0.0.1:5433 (`make up`); tests use an isolated
# `<POSTGRES_DB>_test` database. Override with TEST_POSTGRES_HOST/PORT, FORK_URL_OPTIMISM.
test-integration:
	RUN_INTEGRATION=1 uv run pytest tests/integration $(ARGS)

check: format-check lint typecheck test

migrate:
	uv run alembic upgrade head

up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=200

ps:
	$(COMPOSE) ps

# Compare configured RPC endpoints (latency + head freshness); uses your .env
rpc-latency:
	uv run python -m aegis.tools.rpc_latency

# Liquidation market report from the collected data (needs the stack running).
# Writes Markdown + JSON + CSV exports to data/market/ (git-ignored). Usage: make market-report DAYS=14
DAYS ?= 30
MARKET_DIR = data/market
market-report:
	mkdir -p $(MARKET_DIR)
	$(COMPOSE) exec -T scanner python -m aegis.tools.market_report --days $(DAYS) > $(MARKET_DIR)/report.md
	$(COMPOSE) exec -T scanner python -m aegis.tools.market_report --days $(DAYS) --format json > $(MARKET_DIR)/report.json
	$(COMPOSE) exec -T scanner python -m aegis.tools.market_report --days $(DAYS) --format csv --table events > $(MARKET_DIR)/liquidations.csv
	$(COMPOSE) exec -T scanner python -m aegis.tools.market_report --days $(DAYS) --format csv --table sightings > $(MARKET_DIR)/sightings.csv
	@echo "Market report written to $(MARKET_DIR)/ (report.md, report.json, liquidations.csv, sightings.csv)"
