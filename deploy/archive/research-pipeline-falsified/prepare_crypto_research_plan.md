# Crypto Research Plan

## Why this is separate from current sports research

The current quote/backtest set is dominated by sports outright / season-long markets.
Those conclusions should not be assumed to transfer directly to short-horizon crypto markets.

Crypto up/down 5m style markets likely differ in:
- quote update frequency
- short-horizon movement
- passive fill likelihood
- spread behavior around rapid repricing

## Current artifacts

- `data/fillability_watchlist.json` → sports-derived top fillability list from current quote set
- `data/crypto_event_watchlist.json` → crypto short-horizon event scaffold for the next research lane

## Recommended next steps

1. Resolve crypto event URLs into active token ids / condition ids
2. Build a crypto-specific quote watchlist JSON in the same shape used by `deploy.collect_quotes`
3. Run quote collection on a small crypto basket only
4. Run the same analyses on the crypto basket:
   - fill likelihood
   - long-entry diagnostics
   - mean-reversion diagnostics
   - execution scenario analysis
5. Compare crypto vs sports on:
   - spread
   - passive fill proxy
   - short-horizon move size
   - maker sensitivity

## Selection rule for first crypto batch

Start with a tiny basket only:
- BTC up/down 5m
- ETH up/down 5m
- SOL up/down 5m

Optional later:
- XRP, DOGE

## Research goal

Answer this before more live probing:
- are crypto short-horizon markets materially more fillable and more executable than the current sports outright set?
