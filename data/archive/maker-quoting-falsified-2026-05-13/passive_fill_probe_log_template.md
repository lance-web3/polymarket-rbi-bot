# Passive Fill Probe Log Template

Copy one block per probe.

---

## Probe ID
- Date:
- Probe number:
- Purpose: execution test only

## Market
- Question:
- Condition ID:
- Token ID:
- Outcome side:
- Opposite side:

## Entry Setup
- Intended size:
- Order type: post-only
- Best bid at post:
- Best ask at post:
- Spread bps at post:
- Posted price:
- Placement timestamp:
- Cancel timeout:

## Entry Result
- Posted successfully: yes / no
- Fill result: none / partial / full
- First fill timestamp:
- Time to first fill:
- Filled size ratio:
- Notes on queue / market movement:

## Entry Markout
- 1 minute markout:
- 5 minute markout:
- 15 minute markout:
- Toxic fill suspicion: yes / no
- Why:

## Exit Setup
- Exit posted: yes / no
- Exit best bid at post:
- Exit best ask at post:
- Exit spread bps at post:
- Exit posted price:
- Exit placement timestamp:
- Exit timeout:

## Exit Result
- Exit fill result: none / partial / full
- Exit first fill timestamp:
- Exit time to first fill:
- Exit filled size ratio:
- Forced fallback used: yes / no
- Fallback details:

## Round-Trip Outcome
- Realized pnl:
- Approx return bps:
- Resting-time acceptable: yes / no
- Passive fill quality acceptable: yes / no
- Would repeat this market: yes / no

## Verdict
- What did this probe teach us?
- Did passive execution look realistically attainable?
- Next action:
