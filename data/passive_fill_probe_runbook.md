# Passive Fill Probe Runbook (Probe #1)

This runbook is for the **first live passive-fill execution test**.

Goal:
- learn whether passive fills are realistically attainable
- avoid mixing execution testing with strategy discovery
- keep risk and improvisation near zero

## Probe #1 constraints

- tiny notional only, around **$1 to $10 max**
- one market only
- one order resting at a time
- no averaging down
- no chasing price
- no automatic retries
- bounded timeout

## Step 0: Confirm mindset

Say this explicitly before starting:
- "This is an execution probe, not a trading attempt."

If you feel tempted to widen size, chase, or improvise, stop.

## Step 1: Choose one candidate market

Pick only one market that looks relatively suitable:
- tighter spread than the other candidates
- active enough that quotes move and fills are plausible
- not obviously chaotic
- not near resolution disorder

Record in the log template:
- question
- condition id
- token id
- chosen outcome side

## Step 2: Pre-flight checks

Before placing anything, confirm:
- credentials are working
- reconciliation is healthy
- quote is fresh
- best bid / ask are visible
- spread is acceptable for the probe
- you know your timeout before posting
- you know what you will do if nothing fills

If any of these are not true, stop.

## Step 3: Define exact probe parameters before posting

Write these down before placing the order:
- side
- size
- posted price
- post-only: yes
- timeout: for example **5 to 15 minutes max**
- fallback if no fill: cancel and stop
- fallback if entry fills but exit later fails: pause and review, do not improvise

Important:
- do not decide these after the order is already resting

## Step 4: Observe quote context

Immediately before posting, record:
- timestamp
- best bid
- best ask
- spread bps
- your posted price relative to best bid / ask

If spread suddenly widens or the book looks stale, stop.

## Step 5: Place one passive entry order

Post one tiny **post-only** order.

Right after placing:
- confirm the order posted successfully
- confirm it did not cross the spread
- record placement timestamp
- start timing the fill wait

## Step 6: During resting period

While the order is resting:
- do not add another order
- do not move price repeatedly
- do not convert into taker out of impatience

Only observe and record:
- does market move away immediately?
- does order get partial fill?
- does it get full fill?
- does nothing happen?
- how long until first fill?

If timeout is reached without fill:
- cancel
- record no-fill outcome
- stop the probe

A no-fill result is still useful data.

## Step 7: If entry fills

If the entry gets filled:
- record fill time
- record filled size ratio
- note whether the fill looked benign or toxic
- record 1m / 5m / 15m markout if possible

Do not rush to prove anything. Just log what happened.

## Step 8: Optional passive exit

Only if you are comfortable and the situation is still clean:
- place one passive exit order
- again use post-only
- again define timeout before posting
- again record quote context before posting

If exit does not fill in time:
- do not improvise repeatedly
- pause and review
- use the pre-decided safe fallback only if necessary

## Step 9: End the probe cleanly

At probe end, record one of:
- no fill
- partial fill only
- entry fill but no exit fill
- full round trip completed

Then record:
- realized pnl if any
- whether passive fill looked plausible
- whether this market deserves another probe
- what was learned

## Step 10: Review before probe #2

Do not immediately repeat.

Ask:
- did the order actually rest as intended?
- did it fill often enough to matter?
- was the fill toxic?
- would taker execution have been obviously worse?
- did the probe answer the execution question at all?

Only after review should you decide whether to run probe #2.

## Red lines

Stop immediately if any of these happen:
- reconciliation or state tracking becomes unreliable
- you feel pressure to chase or "just try one more"
- order behavior is confusing or unexpected
- fills appear systematically toxic
- operational complexity is rising faster than insight

## Best possible outcome for Probe #1

Not "we made money".

Best outcome is:
- the order rested correctly
- the fill behavior was understandable
- the logging is complete
- we learned something real about passive execution feasibility
