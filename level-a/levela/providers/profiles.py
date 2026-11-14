"""Behaviour profiles: how each simulated party of the long run behaves (V2 section 8).

Every simulated party in scenarios/recall_long.json has an entry in
`sim_truth`: a profile name, a ground truth that conserves (what really
happened to its panels) and the profile's parameters. This module turns one
contact into the party's reply. It is pure (no database, no clock, no
threads), so every behaviour is tested without a network; SimNetwork plays
the reply on a call and drives the party-initiated events.

| profile        | on a call                                                      |
|----------------|----------------------------------------------------------------|
| straight       | answers the first call with the truth                          |
| no_answer      | rings out or is busy until `answer_on_attempt`, or outside     |
|                | `answer_hours` (once reached, it always answers)               |
| callback       | the first time, asks to be called back at `callback_hour` the  |
|                | next day; after that answers with the truth                    |
| over_reporter  | first reports `over_report_by` more in stock than it received, |
|                | and after the database refuses that, the truth (one call)      |
| reviser        | answers the truth; `revise.after_hours` later it sends a       |
|                | corrected count through its form (`revise.delta` panels moved  |
|                | from in stock to returned)                                     |
| forwarder      | reports its forwards (forwarded_qty + forwarded_to_name for    |
|                | one destination, a `forwards` list for several); with          |
|                | `misreport` it first claims `extra` more went to one party     |
|                | (taken from in stock) and gives the truth on the follow-up     |
| short_receiver | reports what it really received, `short_by` fewer than its     |
|                | sender first claims, and stands by it on follow-up             |
| unreachable    | never answers and ignores form links; the distributor's rep    |
|                | reports for it at `rep_reports_at` once the party is escalated |

Truth: `{received? (tiers 2-3), in_stock, installed, returned,
forwards: [{to_party_id, to_name, qty}], fix?, other_batches: {batch: count}}`.
An installer installs panels at a building owner's site, which is a forward
to the owner. A building owner's truth has its panels `installed` and a fix
script, `fix: {at, model, version_before, version_after}`: before simulated
hour `at` its panels are installed, the display shows `version_before` and
the sounder does not ring on the test; from `at` on (the installer has
visited) they are fixed, the display shows `version_after` and the sounder
rings. An owner answers the three checks on every call (model_seen,
version_seen, sounder_test), so a fix check after `at` books them fixed.

Questions are answered from the truth: a question naming a batch ("Did you
also receive batch L-4480?") is answered from `other_batches`. Values by
answer_type: yes_no_count -> {"answer": "yes"|"no", "count": n}, yes_no ->
"yes"|"no", count -> n, anything else -> a sentence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..clock import hour_of_day, next_morning

PROFILES = ("straight", "no_answer", "callback", "over_reporter", "reviser", "forwarder", "short_receiver",
            "unreachable")
PURPOSES = ("initial", "retry", "receipt", "dispute_followup", "ask_question", "fix_check")

ANSWER, NO_ANSWER, BUSY, CALLBACK = "answer", "no_answer", "busy", "callback"

BUCKETS = ("in_stock", "installed", "fixed", "returned")
DEFAULT_ANSWER_ON_ATTEMPT = 3       # a no_answer party with no stated hours
DEFAULT_CALLBACK_HOUR = 9
DEFAULT_FORM_AFTER_H = 2.0          # a party that answers its phone also fills in a form it is sent
BATCH = re.compile(r"\b[A-Z]{1,3}-\d{3,5}\b")


@dataclass(frozen=True)
class Context:
    """What the party knows when its phone rings.

    attempt: how many times this party's phone has rung in this recall,
    this call included. reached_before: an earlier call was picked up.
    callbacks_before: earlier calls that ended with a callback request.
    open_questions: [{id, text, answer_type}]. told_qty: the other side's
    number on a dispute follow-up. sender_name: who forwarded panels to it.
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


@dataclass(frozen=True)
class Reply:
    """The party's side of one call.

    kind: answer | no_answer | busy | callback. claims: submit_count tool
    bodies, in the order the party says them (the player stops at the
    first one the database accepts). answers: [{question_id, value}].
    callback_at: the simulated hour to call back. say: what the party says,
    one line per claim, then one per answer, then one for the callback.
    """
    kind: str
    claims: list = field(default_factory=list)
    answers: list = field(default_factory=list)
    callback_at: float | None = None
    say: list = field(default_factory=list)

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
    if profile == "callback" and context.callbacks_before == 0:
        hour = int(params.get("callback_hour", DEFAULT_CALLBACK_HOUR))
        return Reply(kind=CALLBACK, callback_at=next_morning(context.sim_now, hour),
                     say=[f"The manager has the job sheets and is on site until tomorrow. Can you call back "
                          f"tomorrow at {hour}:00?"])
    claims, says = _claims(profile, truth, params, context)
    answers, answer_says = answers_for(context.open_questions, truth)
    says += answer_says
    if says and not context.reached_before and (context.attempt >= 3 or context.ladder_step >= 4):
        says[0] = f"Sorry we missed your calls. {says[0]}"
    return Reply(kind=ANSWER, claims=claims, answers=answers, say=says)


def _pickup(profile: str, params: dict, ctx: Context) -> str:
    """answer, or the reason the call did not connect. A party that never
    answers alternates ringing out and an engaged line, as real ones do."""
    if profile == "unreachable":
        return NO_ANSWER
    if profile != "no_answer" or ctx.reached_before:
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
    told, now = ctx.told_qty, ctx.sim_now
    if ctx.purpose == "dispute_followup":
        body = claim_body(truth, sim_now=now)
        return [body], [_followup_line(profile, truth, params, told, now)]
    if profile == "over_reporter" and not ctx.reached_before:
        wrong = claim_body(truth, sim_now=now, in_stock_extra=int(params.get("over_report_by", 0)))
        right = claim_body(truth, sim_now=now)
        return [wrong, right], [spoken(wrong, ctx.sender_name),
                                "Sorry, I was reading the wrong line. " + spoken(right, ctx.sender_name)]
    misreport = params.get("misreport") if profile == "forwarder" and ctx.purpose != "fix_check" else None
    body = claim_body(truth, sim_now=now, misreport=misreport)
    return [body], [spoken(body, ctx.sender_name)]


def _followup_line(profile: str, truth: dict, params: dict, told: int | None, sim_now: float) -> str:
    """A forwarder that over-reported checks its job sheets and corrects;
    everyone else stands by its count."""
    lies = {m["to_party_id"]: m["extra"] for m in params.get("misreport") or []} if profile == "forwarder" else {}
    corrected = [f for f in truth.get("forwards") or [] if f["to_party_id"] in lies]
    body = claim_body(truth, sim_now=sim_now)
    if corrected:
        extra = sum(lies[f["to_party_id"]] for f in corrected)
        sent = ", ".join(f"{f['qty']} went to {f['to_name']}" for f in corrected)
        return f"Checked the job sheets. {sent}. The other {extra} are still in our stock room. " + spoken(body)
    if "received" in truth and told is not None and told != truth["received"]:
        return f"No, {truth['received']}. I have the installer's invoice. " + spoken(body)
    return "Yes, those numbers are right. " + spoken(body)


# ------------------------------------------------------------------ bodies

def fixed_by(truth: dict, sim_now: float | None) -> bool:
    """Whether a building owner's installer has updated its panels by sim_now."""
    fix = truth.get("fix")
    return bool(fix) and sim_now is not None and sim_now >= float(fix["at"])


def claim_body(truth: dict, *, sim_now: float | None = None, in_stock_extra: int = 0,
               misreport: list | None = None) -> dict:
    """The submit_count body for a truth at simulated hour sim_now (without
    the call's ids).

    in_stock_extra inflates in stock (an over-report). misreport moves `extra`
    panels from in stock to the forward to one party (a sender's first-call
    error). A building owner (a truth with a fix script) moves its installed
    panels to fixed once the fix is done, and answers the three checks.
    """
    body = {"received_qty": int(truth["received"])} if "received" in truth else {}
    body.update({b: int(truth.get(b, 0)) for b in BUCKETS})
    fix = truth.get("fix")
    if fix:
        done = fixed_by(truth, sim_now)
        if done:
            body["fixed"] += body["installed"]
            body["installed"] = 0
        body.update(model_seen=fix["model"], version_seen=fix["version_after" if done else "version_before"],
                    sounder_test=done)
    body["in_stock"] += in_stock_extra
    forwards = [dict(f) for f in truth.get("forwards") or []]
    for lie in misreport or []:
        for f in forwards:
            if f["to_party_id"] == lie["to_party_id"]:
                f["qty"] += lie["extra"]
                body["in_stock"] -= lie["extra"]
    if len(forwards) == 1:
        body["forwarded_qty"], body["forwarded_to_name"] = forwards[0]["qty"], forwards[0]["to_name"]
    elif forwards:
        body["forwards"] = [{"to_name": f["to_name"], "qty": f["qty"]} for f in forwards]
    return body


def spoken(body: dict, sender_name: str = "") -> str:
    """The party's sentence for a claim body."""
    parts = []
    if "received_qty" in body:
        source = f" from {sender_name}" if sender_name else ""
        parts.append(f"We received {body['received_qty']}{source}.")
    held = [f"{body[k]} {label}" for k, label in (("in_stock", "in stock"), ("installed", "installed"),
                                                  ("fixed", "updated"), ("returned", "sent back"))
            if body.get(k)]
    parts.append((", ".join(held) + ".") if held else "None left in stock.")
    sent = ([(body["forwarded_qty"], body["forwarded_to_name"])] if body.get("forwarded_qty")
            else [(f["qty"], f["to_name"]) for f in body.get("forwards") or []])
    if sent:
        items = [f"{qty} to {name}" for qty, name in sent]
        joined = items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]
        parts.append(f"We installed or passed on {joined}.")
    if "version_seen" in body:
        rang = "rang" if body.get("sounder_test") else "did not ring"
        parts.append(f"The front says {body['model_seen']}, the display shows {body['version_seen']}, "
                     f"and the sounder {rang} on the test.")
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
    return f"Yes, we got {count} panels of batch {batch}." if count else f"No, we never had batch {batch}."


# ------------------------------------------------------------------ party-initiated events

def form_fields(truth: dict, sim_now: float) -> dict | None:
    """The form submission for a truth at simulated hour sim_now, or None
    when it does not fit the form (the form holds one forward destination)."""
    body = claim_body(truth, sim_now=sim_now)
    return None if "forwards" in body else body


def form_plan(profile: str, truth: dict, params: dict | None, sim_now: float) -> FormPlan | None:
    """Whether and when a party submits through a form link delivered at
    sim_now. None: the party ignores the link."""
    params = params or {}
    if profile == "unreachable":
        return None
    after = params.get("form_after_hours", DEFAULT_FORM_AFTER_H)
    if after is None:
        return None
    submit_at = sim_now + float(after)
    fields = form_fields(truth, submit_at)
    return None if fields is None else FormPlan(submit_at=submit_at, fields=fields)


def revision_plan(profile: str, truth: dict, params: dict | None, accepted_at: float) -> FormPlan | None:
    """A reviser's correction, sent through its form `revise.after_hours`
    after its count was accepted: `revise.delta` panels that were in stock
    have since gone back to the supplier."""
    revise = (params or {}).get("revise")
    if profile != "reviser" or not revise:
        return None
    submit_at = accepted_at + float(revise["after_hours"])
    fields = form_fields(revised_truth(truth, int(revise["delta"])), submit_at)
    return FormPlan(submit_at=submit_at, fields=fields) if fields else None


def revised_truth(truth: dict, delta: int) -> dict:
    if not 0 < delta <= int(truth.get("in_stock", 0)):
        raise ValueError(f"a revision cannot move {delta} panels from {truth.get('in_stock', 0)} in stock")
    return {**truth, "in_stock": truth["in_stock"] - delta, "returned": int(truth.get("returned", 0)) + delta}


def rep_report_at(profile: str, params: dict | None) -> float | None:
    """When the distributor's rep reports for this party once it has been
    escalated (only the unreachable profile has a rep report)."""
    at = (params or {}).get("rep_reports_at")
    return float(at) if profile == "unreachable" and at is not None else None


def rep_report(truth: dict, sim_now: float) -> dict:
    """The counts the rep reports at sim_now: the distributor's own record of the party."""
    return claim_body(truth, sim_now=sim_now)
