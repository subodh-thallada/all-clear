"""Behaviour profiles: how each simulated party of the long run behaves
(spec "Long run", "Scenario at scale").

Every simulated party in scenarios/recall_long.json has an entry in
`sim_truth`: a profile name, a ground truth that conserves (what really
happened to its tins) and the profile's parameters. This module turns one
contact into the party's reply. It is pure (no database, no clock, no
threads), so every behaviour is tested without a network; SimNetwork plays
the reply on a call and drives the party-initiated events.

| profile        | on a call                                                      |
|----------------|----------------------------------------------------------------|
| straight       | answers the first call with the truth                          |
| no_answer      | rings out or is busy until `answer_on_attempt`, or outside     |
|                | `answer_hours` (once in touch, it always answers)              |
| callback       | the first time, asks to be called back at `callback_hour` the  |
|                | next day; after that answers with the truth                    |
| over_reporter  | first reports `over_report_by` more on the shelf than it       |
|                | received, and after the database refuses that, the truth       |
| reviser        | answers the truth; `revise.after_hours` later it sends a       |
|                | corrected count through its form (`revise.delta` tins moved    |
|                | from held to returned)                                         |
| forwarder      | reports its forwards (forwarded_qty + forwarded_to_name for    |
|                | one destination, a `forwards` list for several); with          |
|                | `misreport` it first claims `extra` more went to one store     |
|                | (taken from its shelf count) and gives the truth on follow-up  |
| short_receiver | reports what it really received, `short_by` fewer than its     |
|                | sender first claims, and stands by it on follow-up             |
| unreachable    | never answers and ignores form links; the distributor's rep    |
|                | reports for it at `rep_reports_at` once the party is escalated |
| own_copy       | Summit Foods: its own copy of the agent reports (`reports`);   |
|                | a phone call only hears that its agent is handling the recall  |

Truth: `{received? (tiers 2-3), sold, held, returned, forwards: [{to_party_id,
to_name, qty}], removal?: {after_h, batch}, other_batches: {batch: count}}`.
The removal script: a store's `held` tins stand on the shelf until staff
pull them `removal.after_h` simulated hours after the store first heard of
the recall (`heard_at`: its first answered call or its form link). Before
that a claim reports them `on_shelf` and the till does not refuse the sale;
from then on they are `held`, the batch code is read from a tin and the till
refuses the sale (`block_test: true`). Sold counts are always read from the
till's item report (`sold_source: "till_report"`). A claim that leaves tins
on the shelf sets the party's removal check (the ledger's
`removal_check_at`), and the call it brings (purpose `removal_check`) hears
the counts as they stand then.

Questions are answered from the truth: a question naming a batch ("Did you
also receive batch B-4480?") is answered from `other_batches`. Values by
answer_type: yes_no_count -> {"answer": "yes"|"no", "count": n}, yes_no ->
"yes"|"no", count -> n, anything else -> a sentence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..clock import hour_of_day, next_morning

PROFILES = ("straight", "no_answer", "callback", "over_reporter", "reviser", "forwarder", "short_receiver",
            "unreachable", "own_copy")
PURPOSES = ("initial", "retry", "receipt", "dispute_followup", "ask_question", "removal_check")
REMOVAL_CHECK = "removal_check"

ANSWER, NO_ANSWER, BUSY, CALLBACK = "answer", "no_answer", "busy", "callback"

BUCKETS = ("sold", "on_shelf", "held", "returned")
TILL_REPORT = "till_report"
DEFAULT_ANSWER_ON_ATTEMPT = 3       # a no_answer party with no stated hours
DEFAULT_CALLBACK_HOUR = 9
DEFAULT_FORM_AFTER_H = 2.0          # a party that answers its phone also fills in a form it is sent
BATCH = re.compile(r"\b[A-Z]{1,3}-\d{3,5}\b")
OWN_COPY_LINE = ("Our own recall agent is handling this batch for all our stores. It reports our counts to you "
                 "directly.")


@dataclass(frozen=True)
class Context:
    """What the party knows when its phone rings.

    attempt: how many times this party's phone has rung in this recall,
    this call included. reached_before: an earlier call was picked up.
    callbacks_before: earlier calls that ended with a callback request.
    heard_at: the simulated hour the party first heard of the recall from
    an earlier contact (answered call or form link); None when this call is
    the first. open_questions: [{id, text, answer_type}]. told_qty: the
    other side's number on a dispute follow-up. sender_name: who passed tins
    to it.
    """
    purpose: str = "initial"
    attempt: int = 1
    ladder_step: int = 1
    sim_now: float = 9.0
    told_qty: int | None = None
    open_questions: tuple = ()
    sender_name: str = ""
    reached_before: bool = False
    callbacks_before: int = 0
    heard_at: float | None = None

    def heard(self) -> float:
        """When the party heard of the recall: before this call, or now."""
        return self.sim_now if self.heard_at is None else min(self.heard_at, self.sim_now)


@dataclass(frozen=True)
class Reply:
    """The party's side of one call.

    kind: answer | no_answer | busy | callback. claims: submit_count tool
    bodies, in the order the party says them (the player stops at the
    first one the database accepts). answers: [{question_id, value}].
    callback_at: the simulated hour to call back. say: what the party says,
    one line per claim, then one per answer, then one for the callback.
    note: a line said with no tool call (an own_copy party gives no counts).
    """
    kind: str
    claims: list = field(default_factory=list)
    answers: list = field(default_factory=list)
    callback_at: float | None = None
    say: list = field(default_factory=list)
    note: str = ""                   # a line said with no tool call (an own_copy party's)

    def claim_turns(self) -> list[tuple[dict, str]]:
        return list(zip(self.claims, self.say[:len(self.claims)]))

    def answer_turns(self) -> list[tuple[dict, str]]:
        start = len(self.claims)
        return list(zip(self.answers, self.say[start:start + len(self.answers)]))

    def callback_line(self) -> str:
        return self.say[-1] if self.kind == CALLBACK and self.say else ""


@dataclass(frozen=True)
class FormPlan:
    """A submission the party will make through its form link at `submit_at`
    (simulated hours). `fields` are the form's fields (POST /form/{token})."""
    submit_at: float
    fields: dict


# ------------------------------------------------------------------ calls

def respond(profile: str, truth: dict, params: dict | None, context: Context) -> Reply:
    """The party's reply to one call (pure)."""
    if profile not in PROFILES:
        raise ValueError(f"unknown behaviour profile {profile!r}; choose from {', '.join(PROFILES)}")
    params = params or {}
    pickup = _pickup(profile, params, context)
    if pickup != ANSWER:
        return Reply(kind=pickup)
    if profile == "own_copy":
        answers, answer_says = answers_for(context.open_questions, truth)
        return Reply(kind=ANSWER, answers=answers, say=answer_says, note=OWN_COPY_LINE)
    if profile == "callback" and context.callbacks_before == 0:
        hour = int(params.get("callback_hour", DEFAULT_CALLBACK_HOUR))
        return Reply(kind=CALLBACK, callback_at=next_morning(context.sim_now, hour),
                     say=[f"The manager has the delivery slips and is off until tomorrow. Can you call back "
                          f"tomorrow at {hour}:00?"])
    claims, says = _claims(profile, truth, params, context)
    answers, answer_says = answers_for(context.open_questions, truth)
    says += answer_says
    if says and not context.reached_before and context.heard_at is None and \
            (context.attempt >= 3 or context.ladder_step >= 4):
        says[0] = f"Sorry we missed your calls. {says[0]}"
    return Reply(kind=ANSWER, claims=claims, answers=answers, say=says)


def _pickup(profile: str, params: dict, ctx: Context) -> str:
    """answer, or the reason the call did not connect. A party that never
    answers alternates ringing out and an engaged line, as real ones do. A
    no_answer party that is already in touch (an answered call or a form it
    filled in) picks up its follow-ups."""
    if profile == "unreachable":
        return NO_ANSWER
    if profile != "no_answer" or ctx.reached_before or ctx.heard_at is not None:
        return ANSWER
    on_attempt = params.get("answer_on_attempt")
    hours = params.get("answer_hours")
    if on_attempt is None and not hours:
        on_attempt = DEFAULT_ANSWER_ON_ATTEMPT
    if on_attempt is not None and ctx.attempt >= on_attempt:
        return ANSWER
    if hours and hours[0] <= hour_of_day(ctx.sim_now) < hours[1]:
        return ANSWER
    return BUSY if ctx.attempt % 2 == 0 else NO_ANSWER


def _claims(profile: str, truth: dict, params: dict, ctx: Context) -> tuple[list, list]:
    """(tool bodies, what the party says for each). A question call asks
    nothing about counts, so the party gives none."""
    if ctx.purpose == "ask_question":
        return [], []
    told, now, heard = ctx.told_qty, ctx.sim_now, ctx.heard()
    if ctx.purpose == "dispute_followup":
        body = claim_body(truth, sim_now=now, heard_at=heard)
        return [body], [_followup_line(profile, truth, params, told, body)]
    if profile == "over_reporter" and not ctx.reached_before:
        wrong = claim_body(truth, sim_now=now, heard_at=heard, over_by=int(params.get("over_report_by", 0)))
        right = claim_body(truth, sim_now=now, heard_at=heard)
        return [wrong, right], [spoken(wrong, ctx.sender_name),
                                "Sorry, I was reading the wrong line. " + spoken(right, ctx.sender_name)]
    misreport = params.get("misreport") if profile == "forwarder" and ctx.purpose != REMOVAL_CHECK else None
    body = claim_body(truth, sim_now=now, heard_at=heard, misreport=misreport)
    line = spoken(body, ctx.sender_name)
    if ctx.purpose == REMOVAL_CHECK and pulled(truth, now, heard):
        line = "Staff pulled them. " + line
    return [body], [line]


def _followup_line(profile: str, truth: dict, params: dict, told: int | None, body: dict) -> str:
    """A forwarder that over-reported checks its delivery slips and corrects;
    everyone else stands by its count."""
    lies = {m["to_party_id"]: m["extra"] for m in params.get("misreport") or []} if profile == "forwarder" else {}
    corrected = [f for f in truth.get("forwards") or [] if f["to_party_id"] in lies]
    if corrected:
        extra = sum(lies[f["to_party_id"]] for f in corrected)
        sent = ", ".join(f"{f['qty']} went to {f['to_name']}" for f in corrected)
        return f"Checked the delivery slips. {sent}. The other {extra} are still here. " + spoken(body)
    if "received" in truth and told is not None and told != truth["received"]:
        return f"No, {truth['received']}. I have the delivery slip. " + spoken(body)
    return "Yes, those numbers are right. " + spoken(body)


# ------------------------------------------------------------------ bodies

def pulled(truth: dict, sim_now: float | None, heard_at: float | None) -> bool:
    """Whether staff have pulled the store's tins by sim_now. A truth with no
    removal script (a scripted slice store) is as it ends."""
    removal = truth.get("removal")
    if not removal:
        return True
    if sim_now is None or heard_at is None:
        return False
    return sim_now + 1e-9 >= float(heard_at) + float(removal["after_h"])


def claim_body(truth: dict, *, sim_now: float | None = None, heard_at: float | None = None,
               over_by: int = 0, misreport: list | None = None, done: bool | None = None) -> dict:
    """The submit_count body for a truth at simulated hour sim_now (without
    the call's ids), for a store that heard of the recall at heard_at.

    Before the removal script's pull the store's `held` tins are on the
    shelf; after it they are held, with the batch code read from a tin and a
    till that refuses the sale. `done` forces either state (a rep's report
    is of the store as it ends). over_by inflates the shelf count (an
    over-report). misreport moves `extra` tins from the shelf (or held)
    count to the forward to one store (a sender's first-call error).
    """
    is_pulled = pulled(truth, sim_now, heard_at) if done is None else bool(done)
    body = {"received_qty": int(truth["received"])} if "received" in truth else {}
    tins = int(truth.get("held", 0)) + int(truth.get("on_shelf", 0))
    body.update(sold=int(truth.get("sold", 0)), on_shelf=0 if is_pulled else tins, held=tins if is_pulled else 0,
                returned=int(truth.get("returned", 0)))
    open_bucket = "held" if is_pulled else "on_shelf"
    body[open_bucket] += int(over_by)
    forwards = [dict(f) for f in truth.get("forwards") or []]
    for lie in misreport or []:
        for f in forwards:
            if f["to_party_id"] == lie["to_party_id"]:
                f["qty"] += lie["extra"]
                body[open_bucket] -= lie["extra"]
    if len(forwards) == 1:
        body["forwarded_qty"], body["forwarded_to_name"] = forwards[0]["qty"], forwards[0]["to_name"]
    elif forwards:
        body["forwards"] = [{"to_name": f["to_name"], "qty": f["qty"]} for f in forwards]
    batch = (truth.get("removal") or {}).get("batch") or truth.get("batch")
    body["sold_source"] = TILL_REPORT
    if batch:
        body["batch_seen"] = batch
    body["block_test"] = bool(is_pulled)
    return body


def spoken(body: dict, sender_name: str = "") -> str:
    """The party's sentence for a claim body."""
    parts = []
    if "received_qty" in body:
        source = f" from {sender_name}" if sender_name else ""
        parts.append(f"We received {body['received_qty']}{source}.")
    counts = [f"{body[k]} {label}" for k, label in (("sold", "sold, off the till report"),
                                                    ("on_shelf", "still on the shelf"), ("held", "held in the back"),
                                                    ("returned", "returned")) if body.get(k)]
    parts.append((", ".join(counts) + ".") if counts else "None left in the store.")
    sent = ([(body["forwarded_qty"], body["forwarded_to_name"])] if body.get("forwarded_qty")
            else [(f["qty"], f["to_name"]) for f in body.get("forwards") or []])
    if sent:
        items = [f"{qty} to {name}" for qty, name in sent]
        joined = items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]
        parts.append(f"We passed on {joined}.")
    if body.get("batch_seen"):
        refused = "refused the sale" if body.get("block_test") else "did not refuse the sale"
        parts.append(f"The code under the tin says {body['batch_seen']}, and the till {refused}.")
    return " ".join(parts)


# ------------------------------------------------------------------ questions

def answers_for(questions, truth: dict) -> tuple[list, list]:
    """([{question_id, value}], the party's line for each) for its open questions."""
    return ([{"question_id": q["id"], "value": answer_value(q, truth)} for q in questions],
            [answer_line(q, truth) for q in questions])


def _batch_count(question: dict, truth: dict) -> tuple[str | None, int]:
    match = BATCH.search(question.get("text") or "")
    batch = match.group(0) if match else None
    return batch, int((truth.get("other_batches") or {}).get(batch, 0)) if batch else 0


def answer_value(question: dict, truth: dict):
    """The value submit_answer carries for one open question."""
    batch, count = _batch_count(question, truth)
    kind = question.get("answer_type") or "text"
    if kind == "yes_no_count":
        return {"answer": "yes" if count else "no", "count": count}
    if kind == "yes_no":
        return "yes" if count else "no"
    if kind == "count":
        return count
    return answer_line(question, truth)


def answer_line(question: dict, truth: dict) -> str:
    """What the party says in answer to one question."""
    batch, count = _batch_count(question, truth)
    if batch is None:
        return "No, nothing like that here."
    return f"Yes, we got {count} tins of batch {batch}." if count else f"No, we never had batch {batch}."


# ------------------------------------------------------------------ party-initiated events

def form_fields(truth: dict, sim_now: float, *, heard_at: float | None = None, done: bool | None = None) -> dict | None:
    """The form submission for a truth at simulated hour sim_now, or None
    when it does not fit the form (the form holds one forward destination)."""
    body = claim_body(truth, sim_now=sim_now, heard_at=heard_at, done=done)
    return None if "forwards" in body else body


def form_plan(profile: str, truth: dict, params: dict | None, sim_now: float,
              heard_at: float | None = None) -> FormPlan | None:
    """Whether and when a party submits through a form link delivered at
    sim_now (the store hears of the recall then, unless it already had at
    heard_at). None: the party ignores the link."""
    params = params or {}
    if profile in ("unreachable", "own_copy"):
        return None
    after = params.get("form_after_hours", DEFAULT_FORM_AFTER_H)
    if after is None:
        return None
    submit_at = sim_now + float(after)
    heard = sim_now if heard_at is None else min(heard_at, sim_now)
    fields = form_fields(truth, submit_at, heard_at=heard)
    return None if fields is None else FormPlan(submit_at=submit_at, fields=fields)


def revision_plan(profile: str, truth: dict, params: dict | None, accepted_at: float) -> FormPlan | None:
    """A reviser's correction, sent through its form `revise.after_hours`
    after its count was accepted: `revise.delta` held tins have since gone
    back to the distributor (staff pulled them long before)."""
    revise = (params or {}).get("revise")
    if profile != "reviser" or not revise:
        return None
    submit_at = accepted_at + float(revise["after_hours"])
    fields = form_fields(revised_truth(truth, int(revise["delta"])), submit_at, done=True)
    return FormPlan(submit_at=submit_at, fields=fields) if fields else None


def revised_truth(truth: dict, delta: int) -> dict:
    if not 0 < delta <= int(truth.get("held", 0)):
        raise ValueError(f"a revision cannot move {delta} tins from {truth.get('held', 0)} held")
    return {**truth, "held": truth["held"] - delta, "returned": int(truth.get("returned", 0)) + delta}


def rep_report_at(profile: str, params: dict | None) -> float | None:
    """When the distributor's rep reports for this party once it has been
    escalated (only the unreachable profile has a rep report)."""
    at = (params or {}).get("rep_reports_at")
    return float(at) if profile == "unreachable" and at is not None else None


def rep_report(truth: dict, sim_now: float) -> dict:
    """The counts the rep reports at sim_now: the distributor's record of the
    store after its visit (staff have pulled the tins; the rep read the batch
    code and scanned one at the till)."""
    return claim_body(truth, sim_now=sim_now, done=True)


# ------------------------------------------------------------------ own copy (Summit Foods)

def own_copy_reports(profile: str, params: dict | None) -> list[dict]:
    """The reports an own_copy party's node sends, in seq order (empty for
    every other profile)."""
    if profile != "own_copy":
        return []
    return sorted((params or {}).get("reports") or [], key=lambda r: r["seq"])


def report_due(reports: list[dict], sim_now: float) -> int | None:
    """The index of the latest report due by sim_now (a node that was not
    heard from sends its newest totals, not its backlog), or None."""
    due = [i for i, r in enumerate(reports) if float(r["at"]) <= sim_now + 1e-9]
    return due[-1] if due else None


def report_claim(report: dict, batch: str | None) -> dict:
    """A report as a claim body for the ordinary claim path (until WS5's
    book_report exists): its buckets, the receipt, and the three checks the
    node's own records stand for."""
    b = report["buckets"]
    body = {"received_qty": int(report["units_received"]), **{k: int(b.get(k, 0)) for k in BUCKETS},
            "sold_source": TILL_REPORT, "block_test": True}
    if batch:
        body["batch_seen"] = batch
    return body
