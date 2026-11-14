"""Spec test 5 (close gate), the three metrics and the headline.

try_close with a dispute open is refused by Atlas, again with tins still on
a shelf, and after both are settled (the corrected claim, then the removal
check that finds the tins held) it closes; the three metrics (checks
completed, tins located, held or returned) equal the hand-computed
values in scenarios/recall_1.json (`expected.steps`) after every step, and
the mass balance holds after every write.
"""
from __future__ import annotations

import pytest

from levela.db.seed import load_scenario, start_recall
from levela.ledger.claims import submit_claim
from levela.ledger.reconciler import close_line, headline, mass_balance, metrics, open_units, try_close
from support import HELD_CHECKS, TILL

RID = "RCL-2026-017"


@pytest.fixture
def scenario():
    return load_scenario("recall_1")


def _parse(step: str) -> tuple[str, str, int]:
    """'queen-st-grocer dispute_followup #1' -> (party, phase, index)."""
    party_id, phase, n = step.split()
    return party_id, phase, int(n.lstrip("#")) - 1


def test_5_recall_1_closes_only_when_every_tin_is_found_and_off_the_shelf(client, db, scenario, assert_balance):
    start_recall(client, db, scenario, all_sim=True, emit=False)
    refusals = []
    for i, step in enumerate(scenario["expected"]["steps"]):
        name = step["step"]
        if name == "start":
            pass
        elif name == "close":
            res = try_close(db, RID)
            assert res["closed"] is step["closed"] and res["open_units"] == step["open_units"], (i, res)
            if not step["closed"]:
                # Refused by the close-gate validator, with Atlas's own error.
                assert res["atlas_error"]["code"] == 121 and res["atlas_error"]["errInfo"]
                assert db.recalls.find_one({"_id": RID})["status"] == "open"
                refusals.append(res["headline"])
            else:
                assert res["headline"] == "ALL CLEAR" and close_line(res)[1].startswith("ALL CLEAR")
            continue
        else:
            party_id, phase, n = _parse(name)
            raw = scenario["sim"][party_id][phase][n]["claim"]
            res = submit_claim(client, db, recall_id=RID, party_id=party_id, attempt_id=f"att_{party_id}_{phase}",
                               source="sim", raw=raw)
            assert res["accepted"] is step["accepted"], (name, res)
            assert_balance(RID)
        m = metrics(db, RID)
        got = {"open_units": m["open_units"], "checks": m["checks_completed"],
               "located": m["tins_located"][0], "held_or_returned": m["held_or_returned"][0]}
        want = {k: step[k] for k in ("open_units", "checks", "located", "held_or_returned")}
        assert got == want, name
        assert m["mass_balance"] == m["units_total"] == 240
        assert m["tins_located"][1] == m["held_or_returned"][1] == 240
    # Refused with a transfer open, with a dispute and tins on a shelf, with tins on a shelf alone.
    assert refusals == ["NOT ALL CLEAR: 40 tins open (40 not located)",
                        "NOT ALL CLEAR: 40 tins open (25 still on a shelf, 15 disputed)",
                        "NOT ALL CLEAR: 25 tins open (25 still on a shelf)"]
    recall = db.recalls.find_one({"_id": RID})
    assert recall["status"] == "closed" and recall["open_units"] == 0 and recall["closed_at"]
    assert recall.get("last_close_refused_at")
    assert try_close(db, RID) == {"closed": True, "open_units": 0, "already": True, "headline": "ALL CLEAR"}
    late = submit_claim(client, db, recall_id=RID, party_id="harbour-market", attempt_id="att_late", source="sim",
                        raw={"returned": 40})
    assert late["code"] == "recall_closed"


def test_metrics_from_rows_match_metrics_from_the_database(client, db, scenario, assert_balance):
    start_recall(client, db, scenario, all_sim=True, emit=False)
    for party_id, raw in (("queen-st-grocer", scenario["sim"]["queen-st-grocer"]["initial"][0]["claim"]),
                          ("corner-mart", scenario["sim"]["corner-mart"]["initial"][0]["claim"])):
        assert submit_claim(client, db, recall_id=RID, party_id=party_id, attempt_id=f"att_{party_id}",
                            source="sim", raw=raw)["accepted"]
        assert_balance(RID)
    parties = list(db.parties.find({"recall_id": RID}))
    edges = list(db.edges.find({"recall_id": RID}))
    m = metrics(db, RID, parties=parties, edges=edges)
    assert m == metrics(db, RID)
    assert (m["disputed_units"], m["on_shelf"], m["not_located"]) == (15, 25, 120)
    assert m["open_units"] == open_units(db, RID) == 160
    assert (m["tins_located"], m["held_or_returned"]) == ([105, 240], [30, 240])
    assert m["checks_completed"] == [2, 4] and m["status"] == "open"
    assert headline(m) == "NOT ALL CLEAR: 160 tins open (120 not located, 25 still on a shelf, 15 disputed)"


def test_the_headline_names_only_the_parts_that_are_open():
    base = {"status": "open", "disputed_units": 0, "on_shelf": 0, "not_located": 0}
    assert headline({**base, "open_units": 1, "on_shelf": 1}) == "NOT ALL CLEAR: 1 tin open (1 still on a shelf)"
    assert headline({**base, "open_units": 40, "disputed_units": 15, "on_shelf": 25}) == \
        "NOT ALL CLEAR: 40 tins open (25 still on a shelf, 15 disputed)"
    assert headline({**base, "open_units": 640, "not_located": 285, "on_shelf": 355}) == \
        "NOT ALL CLEAR: 640 tins open (285 not located, 355 still on a shelf)"
    assert headline({**base, "open_units": 0}) == "NOT ALL CLEAR: 0 tins open"
    assert headline({**base, "status": "closed", "open_units": 0}) == "ALL CLEAR"


def test_the_close_gate_refuses_a_ledger_whose_mass_balance_is_off(client, db, scenario, assert_balance):
    """open_units cannot see tins that are on no party and no edge (a
    receipt that dropped another sender's corroborated tins did exactly
    that), so a balance short of units_total refuses the close too."""
    start_recall(client, db, scenario, all_sim=True, emit=False)
    for party_id, raw in (("harbour-market", {"sold": 35, "returned": 5, **TILL}),
                          ("queen-st-grocer", {"held": 120, **HELD_CHECKS}),
                          ("lakeside-foods", {"held": 20, "returned": 60, **HELD_CHECKS})):
        assert submit_claim(client, db, recall_id=RID, party_id=party_id, attempt_id=f"att_{party_id}",
                            source="sim", raw=raw)["accepted"]
    assert_balance(RID)
    # Lose 20 tins around the ledger: every party still conserves, nothing is open.
    db.parties.update_one({"recall_id": RID, "party_id": "queen-st-grocer"},
                         {"$inc": {"shipped_in": -20, "held": -20}})
    assert open_units(db, RID) == 0 and mass_balance(db, RID) == 220
    res = try_close(db, RID)
    assert res["closed"] is False and res["mass_balance"] == 220 and "20 tins" in res["reason"]
    recall = db.recalls.find_one({"_id": RID})
    assert recall["status"] == "open" and recall.get("last_close_refused_at")


def test_unknown_recall(db):
    assert mass_balance(db, "RCL-none") == 0 and open_units(db, "RCL-none") == 0
    assert metrics(db, "RCL-none") == {}
    assert try_close(db, "RCL-none")["closed"] is False
