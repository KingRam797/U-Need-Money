# Progress

## Current State
- Shared agent workflow and companion skills installed.
- `codex/jev-paper-quant` adds a one-symbol Jev paper runner, separate from the
  deterministic replay. It uses TypeSafe's typed choice API and public Binance
  US candles, a persisted SQLite portfolio and event log, next-candle-open
  simulated fills, and coded exposure/loss/spread/request guards. No exchange
  credentials or real order path were added.

## Verification
- `.venv/bin/python -m pytest tests/ -q`: 30 passed (2026-09-25).
- Jev's real API and the public market feed have not been called with user keys.

## Blockers
- A TypeSafe API key is needed for a real Jev paper run. Do not store it here.
- Exchange venue, account eligibility, and instrument minimums need selection
  before any future live adapter; forex and indices are out of this increment.

## Next Step
- Validate a small Jev paper session and inspect audit events. Then add an
  offline recorded-decision replay and compare its net results with baselines
  before designing a US spot practice/live adapter and reconciliation.
