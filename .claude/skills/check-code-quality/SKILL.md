---
name: check-code-quality
description: Run ruff format check, ruff lint, and mypy --strict for Aegis Liquidations and summarize problems. Use after any code change or when asked to check code quality / lint / types.
---

# check-code-quality

1. Run from the repo root:
   ```
   make format-check
   make lint
   make typecheck
   ```
   Equivalent commands: `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy src tests`.
2. Summarize results grouped by tool: number of issues, then each issue as `file:line — message`.
3. If the user passed `fix` as an argument (or asked to fix), run `make format` (`ruff format` + `ruff check --fix`), then fix remaining lint and mypy errors by hand, and re-run all three checks until clean.
4. Never silence errors with blanket `# noqa` / `# type: ignore`; if suppression is unavoidable, use a specific code and a one-line reason.
5. Report the final status honestly; if anything still fails, paste the output.
