"""The contact ladder (V2 §5): what a party gets next, as pure functions over
simulated hours.

    step  action                      ends without counts   next timer
    1     call                        -> step 2             +4 h, or the party's memory hour
    2     call                        -> step 3             next day 09:00
    3     form link (sim delivers it) no submission -> 4    +24 h
    4     third call                  -> step 5             +48 h
    5     escalate: the distributor's rep for an account, a person otherwise
                                      -                     none: units stay unresolved

`parties.ladder_step` is the step whose action runs when `next_action_at`
comes due. A reply can set the timer instead: "call back tomorrow at 9"
keeps the step and makes the callback hour the timer. Accepted counts end
the ladder: no timer, unless the claim left panels installed and not fixed,
when the timer is the party's fix check (`fix_check_at`, set by the claim,
48 simulated hours on). If the party still waits on a transfer, the
counterpart's claim wakes it (the claim transaction sets next_action_at).

`memory_hour` is an hour of day the party is known to answer at, learned
from episodes: an answered call's episode keeps the hour it was placed at
(entities.answered_hour), and the memory check before the party's first
contact in a later recall copies the party's own remembered hour onto
parties.memory_hour (levela.memory.retrieve.party_update). It moves the
second call to that hour, which is the point of remembering it: a party that
only answers after 14:00 is called at 14:00 (spec "Contact ladder" step 2).

Nothing here reads the database or the clock, so the timers can be checked
by hand against the table and replayed from a trace.
"""
from __future__ import annotations

from ..clock import next_at_hour, next_morning

CALL, FORM_LINK, ESCALATE = "place_call", "send_form_link", "escalate"
FIRST_STEP, LAST_STEP = 1, 5
ACTIONS = {1: CALL, 2: CALL, 3: FORM_LINK, 4: CALL, 5: ESCALATE}

FIRST_RETRY_H = 4.0      # after the first unanswered call
FORM_WAIT_H = 24.0       # a delivered form link gets a day to be answered
LAST_CALL_WAIT_H = 48.0  # after the third call, before escalating
MORNING_HOUR = 9.0

# Outcomes after which nothing more is owed by this party's ladder.
SETTLED = ("claim_accepted", "escalated", "hold_verified", "accounts_read")
# Outcomes that mean the party has not given its counts on this step.
UNANSWERED = ("no_answer", "claim_rejected", "failed", "form_sent")
COUNTS_ON_BOOKS = ("claimed", "reconciled")


def clamp(step) -> int:
    """A ladder step in 1..5 (parties created before the ladder start at 1)."""
    try:
        value = int(step)
    except (TypeError, ValueError):
        return FIRST_STEP
    return min(max(value, FIRST_STEP), LAST_STEP)


def action_for(step) -> str:
    """The tool the ladder asks for at a step."""
    return ACTIONS[clamp(step)]


def timer_after(step, sim_now: float, *, memory_hour=None) -> float | None:
    """When the next step runs, once `step` ended without counts (the table's
    last column). None after step 5: nothing is scheduled, the units stay
    unresolved and the recall cannot close."""
    step = clamp(step)
    if step == 1:
        if memory_hour is not None:
            # Strictly after now: a party that did not answer at its hour is not
            # rung again a minute later.
            return next_at_hour(sim_now + 1e-6, float(memory_hour) % 24.0)
        return sim_now + FIRST_RETRY_H
    if step == 2:
        return next_morning(sim_now, MORNING_HOUR)
    if step == 3:
        return sim_now + FORM_WAIT_H
    if step == 4:
        return sim_now + LAST_CALL_WAIT_H
    return None


def after(party: dict, outcome: str, sim_now: float, *, callback_at=None,
          memory_hour=None) -> tuple[int, float | None, str | None]:
    """(ladder_step, next_action_at, action) for a party after a run ended
    with `outcome` at simulated hour `sim_now`.

    callback_requested  same step, timer at the requested hour (never in the past)
    claim_accepted      same step, no timer (the ladder is done), or the fix
                        check when panels are still installed and not fixed
    escalated           step 5, no timer
    answer_recorded     no timer once the counts are on the books; otherwise
                        the party still owes its counts and moves on like an
                        unanswered call
    no_answer, claim_rejected, failed, form_sent
                        the next step at the table's timer
    anything else       unchanged step, due again now (the run did not finish
                        its job, so the party must not lose its turn)

    A party handed to a person (the challenge cap, or step 5 without a rep)
    gets no timer whatever the outcome: only a person moves it on.
    """
    step = clamp(party.get("ladder_step"))
    if memory_hour is None:
        memory_hour = party.get("memory_hour")
    if party.get("escalation") == "human":
        return step, None, None
    if outcome == "callback_requested" and callback_at is not None:
        return step, max(float(callback_at), sim_now), action_for(step)
    if outcome == "claim_accepted" and party.get("installed") and party.get("fix_check_at") is not None:
        return step, max(float(party["fix_check_at"]), sim_now), CALL
    if outcome in SETTLED:
        return (LAST_STEP if outcome == "escalated" else step), None, None
    if outcome == "answer_recorded" and party.get("state") in COUNTS_ON_BOOKS:
        return step, None, None
    if outcome in UNANSWERED or outcome == "answer_recorded":
        return advance(step, sim_now, memory_hour=memory_hour)
    return step, sim_now, action_for(step)


def advance(step, sim_now: float, *, memory_hour=None) -> tuple[int, float | None, str | None]:
    """Move on from `step` after it ended without counts."""
    step = clamp(step)
    if step >= LAST_STEP:
        return LAST_STEP, None, None
    nxt = step + 1
    return nxt, timer_after(step, sim_now, memory_hour=memory_hour), action_for(nxt)


def order_system_next(sim_now: float, *, accounts_open: bool) -> float | None:
    """A distributor's next order system sync: inventory is read once per simulated
    day while any of its accounts is unexplained, so the next sync is the next
    morning; with every account reconciled there is nothing left to read."""
    return next_morning(sim_now, MORNING_HOUR) if accounts_open else None
