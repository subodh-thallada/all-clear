"""Spec test 5 (close gate), the three metrics and the headline.

try_close with a dispute open is refused by Atlas, again with panels
installed and not fixed, and after both are settled (the corrected claim,
then the fix check) it closes; the three metrics equal the hand-computed
values in scenarios/recall_1.json (`expected.steps`) after every step, and
the mass balance holds after every write.
"""
from __future__ import annotations

import pytest

from levela.db.seed import load_scenario, start_recall
from levela.ledger.claims import submit_claim
from levela.ledger.reconciler import close_line, headline, mass_balance, metrics, open_units, try_close

RID = "RCL-2026-017"


@pytest.fixture
def scenario():
    return load_scenario("recall_1")


def _parse(step: str) -> tuple[str, str, int]:
    """'queen-st-alarm dispute_followup #1' -> (party, phase, index)."""
    party_id, phase, n = step.split()
    return party_id, phase, int(n.lstrip("#")) - 1


def test_5_recall_1_closes_only_when_every_panel_is_found_and_fixed(client, db, scenario, assert_balance):
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
               "located": m["panels_located"][0], "fixed_or_returned": m["fixed_or_returned"][0]}
        want = {k: step[k] for k in ("open_units", "checks", "located", "fixed_or_returned")}
        assert got == want, name
        assert m["mass_balance"] == m["units_total"] == 240
        assert m["panels_located"][1] == m["fixed_or_returned"][1] == 240
    # Refused with a transfer open, with a dispute and panels installed, with installed panels alone.
    assert refusals == ["NOT ALL CLEAR: 40 panels open (40 not located)",
                        "NOT ALL CLEAR: 40 panels open (15 disputed, 25 awaiting fix)",
                        "NOT ALL CLEAR: 25 panels open (25 awaiting fix)"]
    recall = db.recalls.find_one({"_id": RID})
    assert recall["status"] == "closed" and recall["open_units"] == 0 and recall["closed_at"]
    assert recall.get("last_close_refused_at")
    assert try_close(db, RID) == {"closed": True, "open_units": 0, "already": True, "headline": "ALL CLEAR"}
    late = submit_claim(client, db, recall_id=RID, party_id="harbour-fire", attempt_id="att_late", source="sim",
                        raw={"in_stock": 40})
    assert late["code"] == "recall_closed"


def test_metrics_from_rows_match_metrics_from_the_database(client, db, scenario, assert_balance):
    start_recall(client, db, scenario, all_sim=True, emit=False)
    for party_id, raw in (("queen-st-alarm", scenario["sim"]["queen-st-alarm"]["initial"][0]["claim"]),
                          ("corner-properties", scenario["sim"]["corner-properties"]["initial"][0]["claim"])):
        assert submit_claim(client, db, recall_id=RID, party_id=party_id, attempt_id=f"att_{party_id}",
                            source="sim", raw=raw)["accepted"]
        assert_balance(RID)
    parties = list(db.parties.find({"recall_id": RID}))
    edges = list(db.edges.find({"recall_id": RID}))
    m = metrics(db, RID, parties=parties, edges=edges)
    assert m == metrics(db, RID)
    assert (m["disputed_units"], m["awaiting_fix"], m["not_located"]) == (15, 25, 120)
    assert m["open_units"] == open_units(db, RID) == 160
    assert (m["panels_located"], m["fixed_or_returned"]) == ([105, 240], [30, 240])
    assert m["checks_completed"] == [2, 4] and m["status"] == "open"
    assert headline(m) == "NOT ALL CLEAR: 160 panels open (15 disputed, 25 awaiting fix, 120 not located)"


def test_the_headline_names_only_the_parts_that_are_open():
    base = {"status": "open", "disputed_units": 0, "awaiting_fix": 0, "not_located": 0}
    assert headline({**base, "open_units": 1, "awaiting_fix": 1}) == "NOT ALL CLEAR: 1 panel open (1 awaiting fix)"
    assert headline({**base, "open_units": 39, "disputed_units": 15, "awaiting_fix": 24}) == \
        "NOT ALL CLEAR: 39 panels open (15 disputed, 24 awaiting fix)"
    assert headline({**base, "open_units": 0}) == "NOT ALL CLEAR: 0 panels open"
    assert headline({**base, "status": "closed", "open_units": 0}) == "ALL CLEAR"


def test_the_close_gate_refuses_a_ledger_whose_mass_balance_is_off(client, db, scenario, assert_balance):
    """open_units cannot see panels that are on no party and no edge (a
    receipt that dropped another sender's corroborated panels did exactly
    that), so a balance short of units_total refuses the close too."""
    start_recall(client, db, scenario, all_sim=True, emit=False)
    for party_id, raw in (("harbour-fire", {"in_stock": 35, "returned": 5}),
                          ("queen-st-alarm", {"in_stock": 120}), ("lakeside-systems", {"in_stock": 20, "returned": 60})):
        assert submit_claim(client, db, recall_id=RID, party_id=party_id, attempt_id=f"att_{party_id}",
                            source="sim", raw=raw)["accepted"]
    assert_balance(RID)
    # Lose 20 panels around the ledger: every party still conserves, nothing is open.
    db.parties.update_one({"recall_id": RID, "party_id": "queen-st-alarm"},
                         {"$inc": {"shipped_in": -20, "in_stock": -20}})
    assert open_units(db, RID) == 0 and mass_balance(db, RID) == 220
    res = try_close(db, RID)
    assert res["closed"] is False and res["mass_balance"] == 220 and "20 panels" in res["reason"]
    recall = db.recalls.find_one({"_id": RID})
    assert recall["status"] == "open" and recall.get("last_close_refused_at")


def test_unknown_recall(db):
    assert mass_balance(db, "RCL-none") == 0 and open_units(db, "RCL-none") == 0
    assert metrics(db, "RCL-none") == {}
    assert try_close(db, "RCL-none")["closed"] is False
