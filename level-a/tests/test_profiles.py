"""providers.profiles: each behaviour profile of the long run (V2 section 8).

Pure tests: respond() and the party-initiated plans need no database. The
parties are the generated scenario's own, so the numbers are the ones the
long run plays.
"""
from __future__ import annotations

import pytest

from levela.clock import next_morning
from levela.db.seed import load_scenario
from levela.providers import profiles
from levela.providers.profiles import ANSWER, BUSY, CALLBACK, NO_ANSWER, Context, respond

SCN = load_scenario("recall_long")
TRUTH = SCN["sim_truth"]
BUCKETS = ("in_stock", "installed", "fixed", "returned")
L4480 = {"id": "q-02", "text": "Did you also receive batch B-4480?", "answer_type": "yes_no_count"}


def play(party_id, **context):
    e = TRUTH[party_id]
    return respond(e["profile"], e["truth"], e["params"], Context(**context))


def truth_claim(party_id, sim_now=9.0):
    return profiles.claim_body(TRUTH[party_id]["truth"], sim_now=sim_now)


def total(body):
    return sum(body.get(b, 0) for b in BUCKETS) + body.get("forwarded_qty", 0) + sum(
        f["qty"] for f in body.get("forwards") or [])


# ---------------------------------------------------------------- calls

def test_straight_answers_the_first_call_with_the_truth():
    reply = play("lambton-mills-alarm")
    assert reply.kind == ANSWER and reply.answers == [] and reply.callback_at is None
    (claim,) = reply.claims
    t = TRUTH["lambton-mills-alarm"]["truth"]
    assert claim == {"received_qty": t["received"], **{b: t[b] for b in BUCKETS}}
    assert total(claim) == claim["received_qty"]
    assert reply.say == [profiles.spoken(claim)]
    lakeside = play("lakeside-systems").claims[0]
    assert "received_qty" not in lakeside, "a party on the shipment list confirms no receipt"
    assert (lakeside["in_stock"], lakeside["returned"]) == (20, 60)


def test_a_no_answer_party_answers_only_on_the_third_attempt():
    kinds = [play("elmvale-fire-systems", attempt=n).kind for n in (1, 2, 3, 4)]
    assert kinds == [NO_ANSWER, BUSY, ANSWER, ANSWER]
    assert play("elmvale-fire-systems", attempt=1, reached_before=True).kind == ANSWER, "once reached, it answers"
    third = play("elmvale-fire-systems", attempt=3, ladder_step=4)
    assert third.say[0].startswith("Sorry we missed your calls.")
    assert third.claims == [truth_claim("elmvale-fire-systems")]


def test_a_no_answer_party_answers_only_in_its_hours():
    start, end = TRUTH["morrow-avenue-alarms"]["params"]["answer_hours"]
    assert play("morrow-avenue-alarms", sim_now=9.5, attempt=1).kind == NO_ANSWER
    assert play("morrow-avenue-alarms", sim_now=13.5, attempt=2).kind == (ANSWER if start <= 13.5 < end else BUSY)
    assert play("morrow-avenue-alarms", sim_now=24 + start + 0.25, attempt=5).kind == ANSWER
    assert play("morrow-avenue-alarms", sim_now=24 + end, attempt=5).kind == NO_ANSWER
    assert play("morrow-avenue-alarms", sim_now=48 + end + 1, attempt=6).kind == BUSY


def test_a_callback_party_asks_to_be_called_back_the_next_morning_once():
    hour = TRUTH["cedarbrae-life-safety"]["params"]["callback_hour"]
    first = play("cedarbrae-life-safety", sim_now=10.25)
    assert first.kind == CALLBACK and first.claims == [] and first.answers == []
    assert first.callback_at == next_morning(10.25, hour) == 24 + hour
    assert f"{hour}:00" in first.callback_line()
    late = play("cedarbrae-life-safety", sim_now=23.5)
    assert late.callback_at == 24 + hour, "asked at night, it still means tomorrow morning"
    second = play("cedarbrae-life-safety", sim_now=24 + hour, attempt=2, reached_before=True, callbacks_before=1)
    assert second.kind == ANSWER and second.claims == [truth_claim("cedarbrae-life-safety")]


def test_an_over_reporter_reports_more_than_it_received_then_the_truth():
    by = TRUTH["ridgeway-alarm-co"]["params"]["over_report_by"]
    reply = play("ridgeway-alarm-co")
    wrong, right = reply.claims
    assert total(wrong) == wrong["received_qty"] + by, "Atlas must refuse the first count"
    assert wrong["in_stock"] == right["in_stock"] + by
    assert right == truth_claim("ridgeway-alarm-co") and total(right) == right["received_qty"]
    assert reply.say[1].startswith("Sorry, I was reading the wrong line.")
    assert [t[0] for t in reply.claim_turns()] == [wrong, right]
    assert play("ridgeway-alarm-co", reached_before=True).claims == [right], "a later call has the right sheet"
    assert play("ridgeway-alarm-co", purpose="dispute_followup").claims == [right]


def test_a_forwarder_reports_several_destinations_as_a_list_and_corrects_on_follow_up():
    t = TRUTH["birchmount-fire-protection"]["truth"]
    (lie,) = TRUTH["birchmount-fire-protection"]["params"]["misreport"]
    (first,) = play("birchmount-fire-protection").claims
    said = {f["to_name"]: f["qty"] for f in first["forwards"]}
    real = {f["to_name"]: f["qty"] for f in t["forwards"]}
    assert set(said) == set(real) and "forwarded_qty" not in first
    assert said["Larkspur Residences"] == real["Larkspur Residences"] + lie["extra"]
    assert first["in_stock"] == t["in_stock"] - lie["extra"]
    assert total(first) == first["received_qty"] == t["received"], "the first claim still conserves"
    followup = play("birchmount-fire-protection", purpose="dispute_followup", told_qty=real["Larkspur Residences"])
    assert followup.claims == [truth_claim("birchmount-fire-protection")]
    assert followup.say[0].startswith("Checked the job sheets.") and "Larkspur Residences" in followup.say[0]


def test_a_forwarder_with_one_destination_uses_the_single_fields():
    (claim,) = play("juniper-lane-alarm").claims
    (fwd,) = TRUTH["juniper-lane-alarm"]["truth"]["forwards"]
    assert (claim["forwarded_qty"], claim["forwarded_to_name"]) == (fwd["qty"], "Willow Creek Towers")
    assert "forwards" not in claim and total(claim) == claim["received_qty"]
    assert "We installed or passed on" in play("juniper-lane-alarm").say[0]


def test_a_short_receiver_reports_fewer_than_its_sender_claims_and_stands_by_it():
    sender = TRUTH["larkspur-residences"]["params"]["sender"]
    claimed = next(f["qty"] for f in play(sender).claims[0]["forwards"] if f["to_name"] == "Larkspur Residences")
    (claim,) = play("larkspur-residences", purpose="receipt", sender_name="Birchmount Fire Protection").claims
    assert claim["received_qty"] == claimed - TRUTH["larkspur-residences"]["params"]["short_by"]
    assert "from Birchmount Fire Protection" in play("larkspur-residences", sender_name="Birchmount Fire Protection").say[0]
    followup = play("larkspur-residences", purpose="dispute_followup", told_qty=claimed)
    assert followup.claims == [claim] and followup.say[0].startswith(f"No, {claim['received_qty']}.")


def test_the_unreachable_party_never_answers_and_ignores_forms():
    e = TRUTH["tallpine-fire-safety"]
    kinds = {play("tallpine-fire-safety", attempt=n, sim_now=9 + 5 * n).kind for n in range(1, 8)}
    assert kinds == {NO_ANSWER}
    assert profiles.form_plan(e["profile"], e["truth"], e["params"], 30.0) is None
    assert profiles.rep_report_at(e["profile"], e["params"]) == 201.0
    assert profiles.rep_report(e["truth"], 201.0) == truth_claim("tallpine-fire-safety")
    assert profiles.rep_report_at("straight", e["params"]) is None


def test_a_reviser_answers_the_truth_and_plans_its_correction_48_hours_later():
    e = TRUTH["glenholme-fire-services"]
    delta = e["params"]["revise"]["delta"]
    assert play("glenholme-fire-services").claims == [truth_claim("glenholme-fire-services")]
    plan = profiles.revision_plan(e["profile"], e["truth"], e["params"], accepted_at=30.5)
    assert plan.submit_at == 78.5
    assert plan.fields["in_stock"] == e["truth"]["in_stock"] - delta
    assert plan.fields["returned"] == e["truth"]["returned"] + delta
    assert total(plan.fields) == plan.fields["received_qty"]
    assert profiles.revision_plan("straight", e["truth"], e["params"], 30.5) is None
    with pytest.raises(ValueError, match="cannot move"):
        profiles.revised_truth(e["truth"], e["truth"]["in_stock"] + 1)


# ---------------------------------------------------------------- questions

def test_a_question_call_answers_from_the_truth_and_asks_no_counts():
    count = TRUTH["cedarbrae-life-safety"]["truth"]["other_batches"]["B-4480"]
    questions = (L4480, {"id": "q-03", "text": "How many panels of B-4480?", "answer_type": "count"},
                 {"id": "q-04", "text": "Any of B-4480 left?", "answer_type": "yes_no"},
                 {"id": "q-05", "text": "Anything else to tell us?", "answer_type": "text"})
    reply = play("cedarbrae-life-safety", purpose="ask_question", open_questions=questions, reached_before=True,
                 callbacks_before=1)
    assert reply.kind == ANSWER and reply.claims == []
    assert reply.answers == [{"question_id": "q-02", "value": {"answer": "yes", "count": count}},
                             {"question_id": "q-03", "value": count}, {"question_id": "q-04", "value": "yes"},
                             {"question_id": "q-05", "value": "No, nothing like that here."}]
    assert reply.answer_turns()[0][1] == f"Yes, we got {count} panels of batch B-4480."
    none = play("lakeside-systems", purpose="ask_question", open_questions=(L4480,))
    assert none.answers == [{"question_id": "q-02", "value": {"answer": "no", "count": 0}}]
    assert none.say == ["No, we never had batch B-4480."]


def test_open_questions_ride_along_on_a_counts_call():
    reply = play("guildwood-alarm", open_questions=(L4480,))
    assert len(reply.claims) == 1 and len(reply.answers) == 1
    assert reply.claim_turns() == [(reply.claims[0], reply.say[0])]
    assert reply.answer_turns() == [(reply.answers[0], reply.say[1])]
    assert play("tallpine-fire-safety", purpose="ask_question", open_questions=(L4480,)).answers == []


# ---------------------------------------------------------------- forms

def test_form_plans_follow_the_profile():
    def plan(sid, now=30.0):
        e = TRUTH[sid]
        return profiles.form_plan(e["profile"], e["truth"], e["params"], now)
    hours = plan("morrow-avenue-alarms")
    assert hours.submit_at == 30.0 + TRUTH["morrow-avenue-alarms"]["params"]["form_after_hours"]
    assert hours.fields == truth_claim("morrow-avenue-alarms", hours.submit_at)
    assert plan("elmvale-fire-systems") is None, "it answers the third call instead"
    assert plan("birchmount-fire-protection") is None, "the form holds one destination"
    single = plan("juniper-lane-alarm")
    assert single.fields["forwarded_qty"] == TRUTH["juniper-lane-alarm"]["truth"]["forwards"][0]["qty"]
    assert plan("lambton-mills-alarm").submit_at == 30.0 + profiles.DEFAULT_FORM_AFTER_H
    assert plan("ridgeway-alarm-co").fields == truth_claim("ridgeway-alarm-co", 32.0), "the form gets the truth"


def test_every_simulated_party_eventually_gives_a_count_that_conserves():
    shipped = {s["party_id"]: s["shipped_in"] for s in SCN["parties"]}
    for sid, e in TRUTH.items():
        if e["profile"] == "unreachable":
            continue
        reply, reached, callbacks = None, False, 0
        for n in range(1, 6):
            reply = play(sid, attempt=n, sim_now=24 * n + 9 + 6 * (n % 2), reached_before=reached,
                         callbacks_before=callbacks)
            reached = reached or reply.kind in (ANSWER, CALLBACK)
            callbacks += reply.kind == CALLBACK
            if reply.kind == ANSWER:
                break
        assert reply.kind == ANSWER, sid
        final = reply.claims[-1]
        inflow = final.get("received_qty", shipped.get(sid))
        assert total(final) == inflow, sid


def test_unknown_profile_and_spoken_lines():
    with pytest.raises(ValueError, match="unknown behaviour profile"):
        respond("grumpy", {}, {}, Context())
    assert profiles.spoken({"received_qty": 12, "in_stock": 12, "returned": 0},
                           "Juniper Lane Alarm") == "We received 12 from Juniper Lane Alarm. 12 in stock."
    assert profiles.spoken({"in_stock": 0, "returned": 0}) == "None left in stock."
    assert profiles.spoken({"in_stock": 1, "returned": 2,
                            "forwards": [{"to_name": "A", "qty": 3}, {"to_name": "B", "qty": 4},
                                         {"to_name": "C", "qty": 5}]}) == \
        "1 in stock, 2 sent back. We installed or passed on 3 to A, 4 to B and 5 to C."
    assert profiles.spoken({"installed": 3, "model_seen": "DEMO-7", "version_seen": "2.4", "sounder_test": False}) == \
        "3 installed. The front says DEMO-7, the display shows 2.4, and the sounder did not ring on the test."


# ---------------------------------------------------------------- the fix script

def test_a_building_owner_reports_installed_before_its_fix_and_fixed_after():
    e = TRUTH["maple-court"]
    fix = e["truth"]["fix"]
    before = play("maple-court", sim_now=fix["at"] - 1).claims[0]
    after = play("maple-court", purpose="fix_check", sim_now=fix["at"] + 1, reached_before=True).claims[0]
    assert (before["installed"], before["fixed"]) == (e["truth"]["installed"], 0)
    assert (before["model_seen"], before["version_seen"], before["sounder_test"]) == (
        "DEMO-7", fix["version_before"], False)
    assert (after["installed"], after["fixed"]) == (0, e["truth"]["installed"])
    assert (after["version_seen"], after["sounder_test"]) == ("2.5", True)
    assert total(before) == total(after) == before["received_qty"]
    assert fix["version_before"] in SCN["recall"]["affected_versions"]


def test_only_building_owners_answer_the_three_checks():
    assert "version_seen" not in play("lambton-mills-alarm").claims[0]
    for sid, e in TRUTH.items():
        assert ("fix" in e["truth"]) == (e["type"] == "owner"), sid
