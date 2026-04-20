# Passive Fill Probe Checklist

Use this before each live passive-fill probe.

## Before placing any order

- [ ] This is an **execution test**, not a strategy test
- [ ] Max risk for this probe is within **$1 to $10**
- [ ] No other live probe position is open
- [ ] No other live probe order is resting
- [ ] Exchange credentials / reconciliation are healthy
- [ ] Decision timestamp is fresh
- [ ] Market selected is relatively tight-spread
- [ ] Market selected is active enough for passive fills to be plausible
- [ ] Spread is acceptable at time of posting
- [ ] You know the exact cancel timeout for this probe
- [ ] You know the exact fallback if exit does not fill

## Entry probe

- [ ] Record market / token / outcome
- [ ] Record quoted best bid and best ask before posting
- [ ] Record spread bps before posting
- [ ] Record posted price
- [ ] Confirm order is **post-only**
- [ ] Confirm order size is tiny
- [ ] Record order placement time
- [ ] Start fill timer

## While order is resting

- [ ] Watch whether best bid / ask moves away immediately
- [ ] Watch whether the order gets partial fill / full fill / no fill
- [ ] Record time to first fill
- [ ] Record whether fill looked toxic (filled just before adverse move)
- [ ] Cancel if timeout is reached

## If entry fills

- [ ] Record fill size ratio
- [ ] Record markout after fill at 1m / 5m / 15m if possible
- [ ] Place only one passive exit order
- [ ] Record exit posted price and quote context
- [ ] Use bounded timeout for exit
- [ ] If exit fails to fill, pause and review before forcing anything

## After probe

- [ ] Record final outcome: no fill / partial / full / completed round trip
- [ ] Record realized pnl if round trip completed
- [ ] Record whether passive execution looked plausible
- [ ] Record whether this market should be tested again
- [ ] Do not immediately re-run without reviewing the result

## Stop conditions

Stop further probes immediately if:
- [ ] reconciliation breaks
- [ ] orders fail in unexpected ways
- [ ] fills are consistently toxic
- [ ] passive orders almost never fill
- [ ] exit handling becomes messy or unsafe
