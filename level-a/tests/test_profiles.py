"""providers.profiles: each behaviour profile of the long run (spec "Scenario at
scale"), plus the removal script every simulated store follows.

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
BUCKETS = ("sold", "on_shelf", "held", "returned")
B4480 = {"id": "q-02", "text": "Did you also receive batch B-4480?", "answer_type": "yes_no_count"}


def play(party_id, **context):
    e = TRUTH[party_id]
    return respond(e["profile"], e["truth"], e["params"], Context(**context))


def truth_claim(party_id, sim_now=9.0, heard_at=None):
    return profiles.claim_body(TRUTH[party_id]["truth"], sim_now=sim_now,
                               heard_at=sim_now if heard_at is None else heard_at)


def pulled_claim(party_id):
    return profiles.claim_body(TRUTH[party_id]["truth"], done=True)


def total(body):
    return sum(body.get(b, 0) for b in BUCKETS) + body.get("forwarded_qty", 0) + sum(
        f["qty"] for f in body.get("forwards") or [])


# ---------------------------------------------------------------- calls

def test_straight_answers_the_first_call_with_the_truth():
    reply = play("lambton-mills-market")
    assert reply.kind == ANSWER and reply.answers == [] and reply.callback_at is None
    (claim,) = reply.claims
    t = TRUTH["lambton-mills-market"]["truth"]
    assert claim == {"received_qty": t["received"], "sold": t["sold"], "on_shelf": t["held"], "held": 0,
                     "returned": t["returned"], "sold_source": "till_report", "batch_seen": "B-4471",
                     "block_test": False}, "first contact: the tins are still on the shelf"
    assert total(claim) == claim["received_qty"]
    assert reply.say == [profiles.spoken(claim)]
    lakeside = play("lakeside-foods").claims[0]
    assert "received_qty" not in lakeside, "a party on the shipment list confirms no receipt"
    assert (lakeside["sold"], lakeside["held"], lakeside["on_shelf"]) == (20, 60, 0)
    assert (lakeside["batch_seen"], lakeside["block_test"]) == ("B-4471", True)


def test_a_no_answer_party_answers_only_on_the_third_attempt():
    kinds = [play("elmvale-grocer", attempt=n).kind for n in (1, 2, 3, 4)]
    assert kinds == [NO_ANSWER, BUSY, ANSWER, ANSWER]
    assert play("elmvale-grocer", attempt=1, reached_before=True).kind == ANSWER, "once reached, it answers"
    assert play("elmvale-grocer", attempt=1, heard_at=20.0).kind == ANSWER, "a form it filled in counts as reached"
    third = play("elmvale-grocer", attempt=3, ladder_step=4)
    assert third.say[0].startswith("Sorry we missed your calls.")
    assert third.claims == [truth_claim("elmvale-grocer")]


def test_a_no_answer_party_answers_only_in_its_hours():
    start, end = TRUTH["morrow-avenue-foods"]["params"]["answer_hours"]
    assert play("morrow-avenue-foods", sim_now=9.5, attempt=1).kind == NO_ANSWER
    assert play("morrow-avenue-foods", sim_now=13.5, attempt=2).kind == (ANSWER if start <= 13.5 < end else BUSY)
    assert play("morrow-avenue-foods", sim_now=24 + start + 0.25, attempt=5).kind == ANSWER
    assert play("morrow-avenue-foods", sim_now=24 + end, attempt=5).kind == NO_ANSWER
    assert play("morrow-avenue-foods", sim_now=48 + end + 1, attempt=6).kind == BUSY


def test_a_callback_party_asks_to_be_called_back_the_next_morning_once():
    hour = TRUTH["cedarbrae-fresh-market"]["params"]["callback_hour"]
    first = play("cedarbrae-fresh-market", sim_now=10.25)
    assert first.kind == CALLBACK and first.claims == [] and first.answers == []
    assert first.callback_at == next_morning(10.25, hour) == 24 + hour
    assert f"{hour}:00" in first.callback_line()
    late = play("cedarbrae-fresh-market", sim_now=23.5)
    assert late.callback_at == 24 + hour, "asked at night, it still means tomorrow morning"
    second = play("cedarbrae-fresh-market", sim_now=24 + hour, attempt=2, reached_before=True, callbacks_before=1,
                  heard_at=10.25)
    assert second.kind == ANSWER
    assert second.claims == [truth_claim("cedarbrae-fresh-market", 24 + hour, heard_at=10.25)]


def test_an_over_reporter_reports_more_than_it_received_then_the_truth():
    by = TRUTH["ridgeway-foods"]["params"]["over_report_by"]
    reply = play("ridgeway-foods")
    wrong, right = reply.claims
    assert total(wrong) == wrong["received_qty"] + by, "Atlas must refuse the first count"
    assert wrong["on_shelf"] == right["on_shelf"] + by
    assert right == truth_claim("ridgeway-foods") and total(right) == right["received_qty"]
    assert reply.say[1].startswith("Sorry, I was reading the wrong line.")
    assert [t[0] for t in reply.claim_turns()] == [wrong, right]
    assert play("ridgeway-foods", reached_before=True).claims == [right], "a later call has the right sheet"
    assert play("ridgeway-foods", purpose="dispute_followup").claims == [right]


def test_a_forwarder_reports_several_destinations_as_a_list_and_corrects_on_follow_up():
    t = TRUTH["birchmount-grocery"]["truth"]
    (lie,) = TRUTH["birchmount-grocery"]["params"]["misreport"]
    (first,) = play("birchmount-grocery").claims
    said = {f["to_name"]: f["qty"] for f in first["forwards"]}
    real = {f["to_name"]: f["qty"] for f in t["forwards"]}
    assert set(said) == set(real) and "forwarded_qty" not in first
    assert said["Larkspur Market"] == real["Larkspur Market"] + lie["extra"]
    assert first["on_shelf"] == t["held"] - lie["extra"]
    assert total(first) == first["received_qty"] == t["received"], "the first claim still conserves"
    followup = play("birchmount-grocery", purpose="dispute_followup", told_qty=real["Larkspur Market"])
    assert followup.claims == [truth_claim("birchmount-grocery")]
    assert followup.say[0].startswith("Checked the delivery slips.") and "Larkspur Market" in followup.say[0]
    check = play("birchmount-grocery", purpose="removal_check", sim_now=30.0, heard_at=9.0, reached_before=True)
    assert check.claims == [truth_claim("birchmount-grocery", 30.0, heard_at=9.0)], "a removal check hears the truth"


def test_a_forwarder_with_one_destination_uses_the_single_fields():
    (claim,) = play("juniper-lane-grocery").claims
    (fwd,) = TRUTH["juniper-lane-grocery"]["truth"]["forwards"]
    assert (claim["forwarded_qty"], claim["forwarded_to_name"]) == (fwd["qty"], "Willow Creek Mini Mart")
    assert "forwards" not in claim and total(claim) == claim["received_qty"]
    assert "We passed on" in play("juniper-lane-grocery").say[0]


def test_a_short_receiver_reports_fewer_than_its_sender_claims_and_stands_by_it():
    sender = TRUTH["larkspur-market"]["params"]["sender"]
    claimed = next(f["qty"] for f in play(sender).claims[0]["forwards"] if f["to_name"] == "Larkspur Market")
    (claim,) = play("larkspur-market", purpose="receipt", sender_name="Birchmount Grocery").claims
    assert claim["received_qty"] == claimed - TRUTH["larkspur-market"]["params"]["short_by"]
    assert "from Birchmount Grocery" in play("larkspur-market", sender_name="Birchmount Grocery").say[0]
    followup = play("larkspur-market", purpose="dispute_followup", told_qty=claimed)
    assert followup.claims == [claim] and followup.say[0].startswith(f"No, {claim['received_qty']}.")


def test_the_unreachable_party_never_answers_and_ignores_forms():
    e = TRUTH["tallpine-general-store"]
    kinds = {play("tallpine-general-store", attempt=n, sim_now=9 + 5 * n).kind for n in range(1, 8)}
    assert kinds == {NO_ANSWER}
    assert profiles.form_plan(e["profile"], e["truth"], e["params"], 30.0) is None
    assert profiles.rep_report_at(e["profile"], e["params"]) == 201.0
    report = profiles.rep_report(e["truth"], 201.0)
    assert report == pulled_claim("tallpine-general-store")
    assert (report["on_shelf"], report["held"], report["block_test"]) == (0, e["truth"]["held"], True), \
        "the rep reports the store after staff pulled the tins"
    assert profiles.rep_report_at("straight", e["params"]) is None


def test_a_reviser_answers_the_truth_and_plans_its_correction_48_hours_later():
    e = TRUTH["glenholme-grocery"]
    delta = e["params"]["revise"]["delta"]
    assert play("glenholme-grocery").claims == [truth_claim("glenholme-grocery")]
    plan = profiles.revision_plan(e["profile"], e["truth"], e["params"], accepted_at=30.5)
    assert plan.submit_at == 78.5
    assert plan.fields["held"] == e["truth"]["held"] - delta and plan.fields["on_shelf"] == 0
    assert plan.fields["returned"] == e["truth"]["returned"] + delta
    assert total(plan.fields) == plan.fields["received_qty"]
    assert profiles.revision_plan("straight", e["truth"], e["params"], 30.5) is None
    with pytest.raises(ValueError, match="cannot move"):
        profiles.revised_truth(e["truth"], e["truth"]["held"] + 1)


# ---------------------------------------------------------------- the removal script

def test_every_store_reports_on_the_shelf_first_and_held_after_staff_pull():
    e = TRUTH["maple-court-market"]
    after_h = e["truth"]["removal"]["after_h"]
    assert 4.0 <= after_h <= 12.0
    first = play("maple-court-market", sim_now=20.0).claims[0]
    still = play("maple-court-market", purpose="removal_check", sim_now=20.0 + after_h - 0.1, heard_at=20.0,
                 reached_before=True).claims[0]
    pulled = play("maple-court-market", purpose="removal_check", sim_now=20.0 + after_h, heard_at=20.0,
                  reached_before=True)
    after = pulled.claims[0]
    assert (first["on_shelf"], first["held"], first["block_test"]) == (e["truth"]["held"], 0, False)
    assert still == first
    assert (after["on_shelf"], after["held"], after["block_test"]) == (0, e["truth"]["held"], True)
    assert after["batch_seen"] == SCN["recall"]["batch_code"] and after["sold_source"] == "till_report"
    assert pulled.say[0].startswith("Staff pulled them.")
    assert total(first) == total(after) == first["received_qty"]


def test_every_simulated_store_outside_the_slice_has_a_removal_script():
    for sid, e in TRUTH.items():
        has = "removal" in e["truth"]
        assert has == (e["tier"] > 1 and e["profile"] != "own_copy"), sid


# ---------------------------------------------------------------- own copy

def test_summit_foods_reports_through_its_own_agent():
    e = TRUTH["summit-foods"]
    reply = play("summit-foods", open_questions=(B4480,))
    assert reply.kind == ANSWER and reply.claims == [] and reply.note == profiles.OWN_COPY_LINE
    assert len(reply.answers) == 1 and reply.answer_turns() == [(reply.answers[0], reply.say[0])]
    assert profiles.form_plan(e["profile"], e["truth"], e["params"], 60.0) is None
    reports = profiles.own_copy_reports(e["profile"], e["params"])
    assert [r["seq"] for r in reports] == list(range(1, len(reports) + 1))
    assert profiles.report_due(reports, reports[0]["at"] - 0.5) is None
    assert profiles.report_due(reports, reports[3]["at"] + 1) == 3
    claim = profiles.report_claim(reports[6], "B-4471")
    assert claim == {"received_qty": 120, "sold": 31, "on_shelf": 9, "held": 58, "returned": 22,
                     "sold_source": "till_report", "block_test": True, "batch_seen": "B-4471"}
    assert reports[-1]["state"] == "closed" and profiles.own_copy_reports("straight", e["params"]) == []


# ---------------------------------------------------------------- questions

def test_a_question_call_answers_from_the_truth_and_asks_no_counts():
    count = TRUTH["cedarbrae-fresh-market"]["truth"]["other_batches"]["B-4480"]
    questions = (B4480, {"id": "q-03", "text": "How many tins of B-4480?", "answer_type": "count"},
                 {"id": "q-04", "text": "Any of B-4480 left?", "answer_type": "yes_no"},
                 {"id": "q-05", "text": "Anything else to tell us?", "answer_type": "text"})
    reply = play("cedarbrae-fresh-market", purpose="ask_question", open_questions=questions, reached_before=True,
                 callbacks_before=1)
    assert reply.kind == ANSWER and reply.claims == []
    assert reply.answers == [{"question_id": "q-02", "value": {"answer": "yes", "count": count}},
                             {"question_id": "q-03", "value": count}, {"question_id": "q-04", "value": "yes"},
                             {"question_id": "q-05", "value": "No, nothing like that here."}]
    assert reply.answer_turns()[0][1] == f"Yes, we got {count} tins of batch B-4480."
    never = next(sid for sid, e in TRUTH.items() if not e["truth"]["other_batches"] and e["profile"] == "straight")
    none = play(never, purpose="ask_question", open_questions=(B4480,))
    assert none.answers == [{"question_id": "q-02", "value": {"answer": "no", "count": 0}}]
    assert none.say == ["No, we never had batch B-4480."]


def test_open_questions_ride_along_on_a_counts_call():
    reply = play("guildwood-grocer", open_questions=(B4480,))
    assert len(reply.claims) == 1 and len(reply.answers) == 1
    assert reply.claim_turns() == [(reply.claims[0], reply.say[0])]
    assert reply.answer_turns() == [(reply.answers[0], reply.say[1])]
    assert play("tallpine-general-store", purpose="ask_question", open_questions=(B4480,)).answers == []


# ---------------------------------------------------------------- forms

def test_form_plans_follow_the_profile():
    def plan(sid, now=30.0, heard=None):
        e = TRUTH[sid]
        return profiles.form_plan(e["profile"], e["truth"], e["params"], now, heard_at=heard)
    hours = plan("morrow-avenue-foods")
    assert hours.submit_at == 30.0 + TRUTH["morrow-avenue-foods"]["params"]["form_after_hours"]
    assert hours.fields == truth_claim("morrow-avenue-foods", hours.submit_at, heard_at=30.0)
    assert plan("elmvale-grocer") is None, "it answers the third call instead"
    assert plan("birchmount-grocery") is None, "the form holds one destination"
    single = plan("juniper-lane-grocery")
    assert single.fields["forwarded_qty"] == TRUTH["juniper-lane-grocery"]["truth"]["forwards"][0]["qty"]
    assert plan("lambton-mills-market").submit_at == 30.0 + profiles.DEFAULT_FORM_AFTER_H
    assert plan("ridgeway-foods").fields == truth_claim("ridgeway-foods", 32.0, heard_at=30.0), \
        "the form gets the truth"
    late = plan("lambton-mills-market", heard=0.0)
    assert late.fields["on_shelf"] == 0 and late.fields["block_test"] is True, "heard long ago: already pulled"


def test_every_simulated_party_eventually_gives_a_count_that_conserves():
    shipped = {s["party_id"]: s["shipped_in"] for s in SCN["parties"]}
    for sid, e in TRUTH.items():
        if e["profile"] in ("unreachable", "own_copy"):
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
    assert profiles.spoken({"received_qty": 12, "on_shelf": 12, "returned": 0},
                           "Juniper Lane Grocery") == "We received 12 from Juniper Lane Grocery. 12 still on the shelf."
    assert profiles.spoken({"sold": 0, "returned": 0}) == "None left in the store."
    assert profiles.spoken({"sold": 1, "returned": 2,
                            "forwards": [{"to_name": "A", "qty": 3}, {"to_name": "B", "qty": 4},
                                         {"to_name": "C", "qty": 5}]}) == \
        "1 sold, off the till report, 2 returned. We passed on 3 to A, 4 to B and 5 to C."
    assert profiles.spoken({"held": 3, "batch_seen": "B-4471", "block_test": True}) == \
        "3 held in the back. The code under the tin says B-4471, and the till refused the sale."
    assert profiles.spoken({"on_shelf": 3, "batch_seen": "B-4471", "block_test": False}).endswith(
        "the till did not refuse the sale.")
