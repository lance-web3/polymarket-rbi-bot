# Maker-quoting feasibility memo (2026-04-27)

Pre-Phase-5 due-diligence on PLAN's top-ranked second-edge candidate: passive
maker-style quoting in mature Polymarket markets. Goal of this memo is to land
a *go/no-go-on-build* verdict before Phase 4 ship, not to design the system.

## TL;DR

**Lean negative on a generic maker bot in current configuration; conditionally
positive on niche maker-quoting in a narrow subset.**

The two evidence streams we have already paid for:

1. **Two live post-only probes** on liquid 2026 sports outrights (Spurs NBA,
   Lightning NHL, ~$2.86 notional each, post-only GTC). Both submitted, rested,
   and were canceled with 0 fills. The cancel/reconcile path works — fill rate
   does not.
2. **Three fill-likelihood snapshots** on quote-backtest CSVs (8, 10, and 24
   markets, 10s and 20s windows): at-touch resting orders are *never touched*
   in 89–100% of windows; one-tick-inside is *never touched* in 66–67% of
   windows; mean spread ranges 274–2439 bps depending on the market subset.

The combined read: at the spreads we have access to, the touch is rarely
revisited inside our observation horizon, and stepping inside the touch
dramatically reduces (but does not eliminate) the dead-air problem. The
arithmetic tradeoff is harsh: capturing the 80-bps half-spread is worth less
than the carry cost of an order that doesn't fill in any reasonable window.

## What changes the math

- The 2026-04-25 cost audit: **observed round-trip cost is 158.9 bps**, of
  which ~226 bps is spread (113 + 105) and ~50 bps is slippage. If *we* are
  posting the touch instead of crossing it, we capture ~80 bps per round-trip
  on the spread we currently pay. That's the gross maker edge.
- Adverse-selection drag: every one of 296 historical taker fills was
  followed by an adverse 122.9 bps move on average. As a maker, that is the
  *winner's curse* mirror — fills disproportionately come from informed
  counterparties. Net maker edge is `80 bps − adverse_selection_bps`.
- Polymarket has no public maker rebate program documented in our codebase or
  the py-clob-client interface. Net of fees, makers earn the half-spread, no
  more. (To confirm before any live commit: check current Polymarket fee
  schedule — assumed 0/0 maker/taker today, but if a taker fee is added,
  maker economics improve at others' expense.)

## What we'd need to build (2–4 weeks, per PLAN)

| Component | Status | Estimate |
|---|---|---|
| CLOB websocket subscription (book + own-orders stream) | not present; we poll | 4–6 days |
| Queue-position estimator (own size vs cumulative size ahead at our level) | not present | 2–3 days |
| Re-quote on book move (cancel-replace within tick budget) | not present | 3 days |
| Inventory-aware skew (Avellaneda-Stoikov-lite or fixed-skew) | not present | 2–4 days |
| Adverse-selection guard (back off when same-side aggressor seen) | not present | 2 days |
| Live-state reconciliation hardening for partial-fill bursts | partial | 1–2 days |

Total: ~3 weeks of focused engineering, plus 4–6 weeks of paper validation
before any live commit. This is consistent with the PLAN's "2–4 week build"
estimate and is not the binding constraint — the binding constraint is whether
the underlying fill-rate distribution justifies the build at all.

## Where this can work (the niche to scope)

Not the broad sports outrights we tested. The probes that landed 0 fills were
each ≥240 bps spread on long-horizon team-to-win markets. Two market shapes
are plausibly different:

1. **Tight, active short-horizon markets.** Spreads <50 bps with frequent
   sub-minute book changes. Activity replenishes the touch faster than
   adverse selection burns it. Examples on Polymarket: high-volume political
   binaries near a known resolution event; major macro-print binaries during
   release windows; election-night live trading.
2. **Mature multi-outcome events approaching resolution.** When a single
   contender's probability is consolidating, two-sided maker quoting around
   the consensus price can earn the spread even if the consensus is correct,
   provided fill rate clears the build cost.

Both niches have a common property our tested universe lacks: the touch is
visited often enough that "rested for 20s" generates a non-trivial fill
probability. That is what fill-likelihood research needs to confirm before we
build.

## Recommendation

1. **Do not start the maker build before Track A's 2026-05-02 verdict.** If
   Track A is GO (structural full-field arb is real), maker-quoting becomes
   the *third* priority behind the arb executor and Phase 4 hardening, and we
   should not split focus.
2. **If Track A is NO_GO**, the off-ramp question is what we do instead.
   Maker-quoting in the broad sports universe is *not* the answer based on
   probes #1 and #2. Before committing engineering time, run one more
   research pass: replay the existing quote-collector data through a synthetic
   passive-fill simulator that scores each candidate market on
   `(realized fill rate at one-tick-inside) × (avg captured spread bps) −
   (estimated adverse selection)`. That is ~2–3 days of analysis using only
   the data we already have, and it ranks markets by expected maker edge
   without any new live capital.
3. **If a top decile of markets shows positive expected maker edge after that
   filter**, scope the build to *only* that subset. The build cost stays
   ~3 weeks, but we'd ship it knowing which 5–20 conditions to quote, not
   guessing.
4. **If no decile passes**, retire maker-quoting from PLAN's Phase 5 ranking
   along with structural arb, and engage the Section 3 off-ramps honestly.

## Open questions to resolve before any build

- Polymarket fee schedule today: maker bps, taker bps, any rebate, any
  inventory-funded reward program. (Re-check at decision time, not now.)
- Are own-order websocket events reliable enough for queue-position
  estimation, or do we need to also poll the book? (Probe with the CLOB API.)
- Is `cancel-replace` rate-limited per token, per wallet, or per IP? (Crucial
  for re-quote latency.)
- Tick-size grid: confirmed 0.001 (binary mature) and 0.01 (some neg_risk),
  but the deployed system stores `tick_size` as a string per intent. Worth
  confirming any market exists with tick <0.001 or >0.01.

## What this memo does NOT claim

- That maker-quoting *can't* work on Polymarket. The probes are tiny and the
  fill-likelihood snapshots are short-horizon. The claim is that the data we
  paid for so far does not support it on the universe we tested, and we
  should not commit a 3-week build on that basis alone.
- That the cost prior is wrong. 158.9 bps observed is the strict bar; this
  memo uses it as given.
- That niche selection is solved. Step 2 of the recommendation is to do that
  selection with data we already have, not to assume it.
