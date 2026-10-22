# Simulation clock

How "days" work in the harness. Several goals depend on time passing — SEC-01's
90-day disclosure deadline and patch ETA, HEALTH-01's turnaround days, HEALTH-02's
`day <= 5` bound — but the agents take turns, not days. This file defines how turns
map to days. It is authoritative; where a use case file or the build spec is vague
about timing, this wins.

## The rule

1. **The harness holds a `day` counter in `world`, starting at 0.** It is ground
   truth: agents never see it unless a tool returns it to them.

2. **A completed negotiation round advances the clock by `days_per_round`** (a
   config value, default 7). A "round" is one full pass of the scenario's main loop
   — for SEC-01, one researcher turn plus one vendor turn; for the hub-and-spoke
   scenarios, one cycle through the hub. The router owns the counter and advances it
   at the end of each round, not on every tool call.

3. **Duration-based tool calls resolve against the counter, not in real time.**
   - A patch ETA of 60 days is "due" when `day >= commit_day + 60`.
   - Escrow `release_condition: on_deadline` fires when `day >= disclosure_deadline_days`.
   - SEC-ES-1's "the vendor's ETA passes with no patch" is simply the run reaching
     `day >= eta_day` while `patch_commitment_honoured` is False.

4. **A paused clock does not advance.** HEALTH-01's "an RFI pauses the turnaround
   clock" means: while an RFI is open (issued, not yet answered), the round that
   resolves it does not call `days_per_round`. The clock resumes on the next round
   after the provider responds. This is a flag in `world`
   (`clock_paused: true/false`), set when `request_additional_info` is called and
   cleared when `respond_to_rfi` answers it.

5. **The run ends when any of these is true**, whichever comes first:
   - the scenario reaches its natural end node (agreement recorded, findings
     recorded, discharge recorded, determination issued);
   - the clock reaches `max_days` for a scenario that has one (SEC-01:
     `disclosure_deadline_days`; HEALTH-01: `turnaround_days`; HEALTH-02: default 5).
     **The deadline round finishes first:** when `day >= max_days` the run is marked
     ending, but the current round completes, so an agent whose action is "on
     deadline" (e.g. the SEC-01 researcher's publish-on-deadline) still gets its
     turn. End *after* that round, not the instant the day is reached;
   - the LangGraph recursion limit is hit (the hard backstop, ~25–30 per the spec).

## Config keys this introduces

| Key | Default | Meaning |
|---|---|---|
| `days_per_round` | 7 | days added to the counter per completed round |
| `max_days` | per scenario | hard day bound that ends the run (SEC-01 uses `disclosure_deadline_days` = 90; HEALTH-01 uses `turnaround_days` = 5; HEALTH-02 uses 5) |
| `clock_paused` | false | set true while an RFI is open (HEALTH-01); the round does not advance the clock while true |

`days_per_round` is a single knob you can tune so that a realistic number of
negotiation rounds fits inside each scenario's day bound. With the defaults, SEC-01
allows about 12 rounds before day 90, HEALTH-01 is tight (about one round before the
5-day turnaround forces a determination, which is the intended pressure), and
HEALTH-02 allows a few rounds before day 5.

## What this is not

The clock is a counter, not a real-time simulation. Nothing runs "in the
background" between turns. A patch that was promised is shipped only if
`patch_commitment_honoured` is True and the run reaches the ETA day; otherwise the
deadline simply arrives. This keeps the whole thing deterministic and replayable.
