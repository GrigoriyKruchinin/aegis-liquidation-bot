---
name: audit-security
description: Security & performance audit of the current changes in Aegis Liquidations — delegates review to the security-critic-agent and runs automated scanners (bandit, pip-audit, Slither for contracts). Use for anything touching keys, RPC, money math, execution, or Solidity.
---

# audit-security

1. Determine scope: `git diff main...HEAD` plus `git diff` (uncommitted). If the user passes a path, restrict to it.
2. Launch the `security-critic-agent` subagent with that scope and the checklist in `.claude/agents/security-critic-agent.md`.
3. Run automated scanners when available (skip with a note if not installed):
   ```
   uv run bandit -r src -q
   uv run pip-audit
   git ls-files | grep -E '(^|/)\.env$' && echo "ERROR: .env is tracked"
   ```
   If `contracts/` exists: `cd contracts && slither . --exclude-dependencies`.
4. Merge agent findings and scanner output into one severity-ranked report (`CRITICAL` → `LOW`) with `file:line`, failure scenario, and suggested fix.
5. Do not apply fixes unless the user asks.
