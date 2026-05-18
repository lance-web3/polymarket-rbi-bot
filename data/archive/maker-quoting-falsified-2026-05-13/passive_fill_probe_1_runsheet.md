# Passive Fill Probe #1 Runsheet

Default candidate: **San Antonio Spurs 2026 NBA Finals market**

Use this sheet during the first live passive-fill execution probe.

## Probe intent

- execution test only
- not a strategy validation test
- tiny size only
- one resting order only

## Candidate market

**Question**
- Will the San Antonio Spurs win the 2026 NBA Finals?

**Condition ID**
- `0xb6b3d7a2037b3faa7e1306d741840d453432902d73cc9a146a035e40271eae73`

**Token IDs**
- Yes: `102227184035967850089766981958743064457339118173548431660886438726896222843254`
- No: `12636035070565821048178968461063687179393834041535317885287743395873720755118`

## Live-side selection rule

At probe time, choose the side with the cleaner live book:
- tighter current spread
- fresher / more active quotes
- clearer best bid / best ask behavior

Selected side for this probe:
- [ ] Yes
- [ ] No

Chosen token ID:
- 

## Probe parameters

- Size / notional:
- Order type: post-only
- Entry side:
- Posted price:
- Timeout:
- Fallback if no fill: cancel and stop
- Fallback if exit later fails: pause and review before forcing anything

## Pre-post quote snapshot

Timestamp:

Best bid:

Best ask:

Spread bps:

Reason this side was chosen:

## Entry order

Entry posted successfully:
- [ ] Yes
- [ ] No

Order placement timestamp:

Order id:

Order resting at:
- [ ] best bid
- [ ] inside spread
- [ ] other

Notes:

## Entry fill observation

First fill timestamp:

Time to first fill:

Fill result:
- [ ] none
- [ ] partial
- [ ] full

Filled size ratio:

Did the market move away immediately after posting?
- [ ] Yes
- [ ] No

Did the fill look toxic?
- [ ] Yes
- [ ] No

Why:

## Markout after entry fill

1 minute:

5 minutes:

15 minutes:

## Optional passive exit

Only complete this section if entry fills and exit is attempted.

Exit posted:
- [ ] Yes
- [ ] No

Exit posted price:

Exit best bid at post:

Exit best ask at post:

Exit spread bps at post:

Exit placement timestamp:

Exit timeout:

Exit fill result:
- [ ] none
- [ ] partial
- [ ] full

Exit first fill timestamp:

Exit time to first fill:

Forced fallback used:
- [ ] Yes
- [ ] No

Fallback details:

## Round-trip result

Realized pnl:

Approx return bps:

Passive execution felt realistically attainable:
- [ ] Yes
- [ ] No

Would test this market again:
- [ ] Yes
- [ ] No

## Final note

Main lesson from probe #1:
