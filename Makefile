.PHONY: install format format-check lint typecheck test test-integration check migrate up-local down-local logs-local ps-local contracts-test

COMPOSE_LOCAL = docker compose -f docker-compose.local.yml

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

# Needs anvil (Foundry) and a migrated PostgreSQL (POSTGRES_* env); FORK_URL_OPTIMISM optional.
test-integration:
	RUN_INTEGRATION=1 uv run pytest tests/integration $(ARGS)

check: format-check lint typecheck test

migrate:
	uv run alembic upgrade head

up-local:
	$(COMPOSE_LOCAL) up -d --build

down-local:
	$(COMPOSE_LOCAL) down

logs-local:
	$(COMPOSE_LOCAL) logs -f --tail=200

ps-local:
	$(COMPOSE_LOCAL) ps

contracts-test:
	cd contracts && forge test -vvv
