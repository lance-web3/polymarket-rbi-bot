# AGENTS.md

## Purpose

This repository contains a Python trading bot starter for Polymarket. The design goal is to keep execution logic modular so strategies, risk controls, and deployment workflows can evolve independently.

## Key Modules

- `strategies/`: Signal generation only. Keep exchange side effects out of strategy classes.
- `backtesting/`: Historical replay and offline evaluation.
- `bot/`: Live order intent creation, exchange connectivity, and risk validation.
- `deploy/`: Thin entry points that glue config, strategies, and runtime actions together.
- `polymarket_rbi_bot/`: Shared models, config loading, and utility functions.

## Development Rules

- Use `py-clob-client` for Polymarket interaction.
- Keep execution limit-order only unless requirements explicitly change.
- Prefer adding new strategy classes over embedding strategy-specific branches in the trader.
- Keep risk checks deterministic and testable.
- Treat this repo as a starter framework: document assumptions whenever behavior depends on external market data or wallet state.
