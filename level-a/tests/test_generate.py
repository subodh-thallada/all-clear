"""scenarios/generate.py and the recall_long.json it writes (spec "Scenario at
scale"; the tin conservation it asserts before writing is test 16's ground).

The invariants are re-checked here by independent arithmetic, not by calling
generate.check(): the profile counts, the order system totals, conservation for
every party and for the batch, the one chain three hops below the shipment
records, short receivers downstream of a forwarder that corrects itself, the
stores' removal scripts, Summit Foods' reports, the connected stores, and
fictional phone numbers. check() itself is tested on broken copies.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import re
from collections import Counter

import pytest

from levela.config import SCENARIO_DIR
from levela.db.seed import load_scenario, start_recall
from levela.ledger.transfers import resolve_party, slugify
from levela.runs import get_run

SEED = 20261114
BUCKETS = ("sold", "on_shelf", "held", "returned")
FICTIONAL = re.compile(r"^\+1(416|647|437|905|289|365)55501\d\d$")
PROFILES = {"straight": 13, "no_answer": 5, "callback": 3, "over_reporter": 3, "reviser": 2, "forwarder": 6,
            "short_receiver": 2, "unreachable": 1, "own_copy": 1}
FORBIDDEN = re.compile(r"panel|installer|sounder|firmware|harbour fire|queen st alarm|corner properties|"
                       r"lakeside systems|kestrel|demo-7|on_hand|\blot\b|portal|fixed_version|\bin_stock\b",
                       re.IGNORECASE)


def _generator():
    spec = importlib.util.spec_from_file_location("recall_long_generate", SCENARIO_DIR / "generate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen = _generator()


@pytest.fixture(scope="module")
def scn():
    return json.loads((SCENARIO_DIR / "recall_long.json").read_text(encoding="utf-8"))


def accounts(scn):
    return [a for p in scn["order_systems"].values() for a in p["accounts"]]


def outflow(truth):
    return sum(truth.get(b, 0) for b in BUCKETS) + sum(f["qty"] for f in truth.get("forwards") or [])


# ---------------------------------------------------------------- the file

def test_the_committed_file_is_what_the_fixed_seed_generates(scn):
    assert scn["seed"] == SEED
    assert gen.build(SEED) == scn, "regenerate: python scenarios/generate.py --seed 20261114"
    assert gen.build(SEED) == gen.build(SEED)
    other = gen.build(SEED + 1)
    gen.check(other)                                         # another seed is still a valid run
    assert other["order_systems"] != scn["order_systems"], "the seed picks the numbers"


def test_no_fire_alarm_or_old_food_word_is_left(scn):
    text = json.dumps(scn)
    assert not FORBIDDEN.findall(text), sorted(set(m.lower() for m in FORBIDDEN.findall(text)))
    mini = (SCENARIO_DIR.parent / "tests" / "fixtures" / "recall_long_mini.json").read_text(encoding="utf-8")
    assert not FORBIDDEN.findall(mini)


def test_recall_and_the_shipment_list(scn):
    r = scn["recall"]
    assert (r["_id"], r["batch_code"], r["unit"], r["units_total"]) == ("RCL-2026-020", "B-4471", "tin", 2400)
    assert (r["product"], r["barcode"], r["defect"], r["affected_batches"]) == (
        "Maple Farm milk powder 800 g", "0 62345 18803 5", "seal_fault", ["B-4471"])
    assert r["barcode_verified"] is False, "the first scan settles it"
    assert r["batch_spoken"] and r["company"] == "Maple Farm Foods" and r["defect_label"] and r["instruction"]
    assert [c["field"] for c in r["checks"]] == ["batch_seen", "sold_source", "block_test"]
    assert (r["plant"]["party_id"], r["plant"]["name"], r["plant"]["tier"]) == (
        "maple-farm-plant", "Maple Farm plant, Guelph", 0)
    base = load_scenario("recall_1")
    assert {k: v for k, v in r.items() if k not in ("_id", "units_total")} == \
        {k: v for k, v in base["recall"].items() if k not in ("_id", "units_total")}, "recall_1's recall"
    parties = {s["party_id"]: s for s in scn["parties"]}
    assert list(parties) == ["harbour-market", "queen-st-grocer", "lakeside-foods", "dist-north", "dist-east"]
    assert all(s["tier"] == 1 for s in parties.values())
    assert [s["type"] for s in parties.values()] == ["store"] * 3 + ["distributor"] * 2
    assert [parties[s]["shipped_in"] for s in ("harbour-market", "queen-st-grocer", "lakeside-foods")] == [40, 120, 80]
    assert sum(s["shipped_in"] for s in parties.values()) == 2400
    for sid, name, order_system, qty in (("dist-north", "Distributor North", "north", 1300),
                                         ("dist-east", "Distributor East", "east", 860)):
        d = parties[sid]
        assert (d["name"], d["channel"], d["order_system"], d["shipped_in"]) == (name, "order_system", order_system,
                                                                                 qty)
        assert d["profile"]
    assert all(parties[s]["slice"] is True for s in ("harbour-market", "queen-st-grocer", "lakeside-foods"))
    corner = scn["directory"][0]
    assert (corner["party_id"], corner["type"], corner["tier"], corner["phone_env"], corner["slice"],
            corner["connected"]) == ("corner-mart", "store", 3, "PHONE_CORNER", True, True)
    assert parties["harbour-market"]["channel"] == "voice" and parties["harbour-market"]["phone_env"] == "PHONE_HARBOUR"
    for s in base["parties"]:
        assert parties[s["party_id"]] == s
    assert corner == base["directory"][0]


def test_the_slice_scripts_end_with_15_disputed_and_25_on_the_shelf(scn):
    sim = scn["sim"]
    lie, truth = sim["harbour-market"]["initial"]
    assert lie["claim"] == {"on_shelf": 60} and lie["expect"]["accepted"] is False, "the first answer is a lie"
    assert sum(truth["claim"].get(b, 0) for b in BUCKETS) == 40
    (queen,) = sim["queen-st-grocer"]["initial"]
    assert {k: queen["claim"][k] for k in ("sold", "held", "forwarded_qty", "forwarded_to_name")} == {
        "sold": 50, "held": 30, "forwarded_qty": 40, "forwarded_to_name": "Corner Mart"}
    assert queen["demo_hold"] is True
    (lakeside,) = sim["lakeside-foods"]["initial"]
    assert (lakeside["claim"]["sold"], lakeside["claim"]["held"]) == (20, 60)
    for claim in (truth["claim"], queen["claim"], lakeside["claim"]):
        assert (claim["sold_source"], claim["batch_seen"], claim["block_test"]) == ("till_report", "B-4471", True)
    (corner,) = sim["corner-mart"]["initial"]
    assert {k: corner["claim"][k] for k in ("received_qty", "on_shelf")} == {"received_qty": 25, "on_shelf": 25}
    assert "removal_check" not in sim["corner-mart"], "in the long run Corner Mart's 25 stay on the shelf"
    assert queen["claim"]["forwarded_qty"] - corner["claim"]["received_qty"] == 15
    base = load_scenario("recall_1")
    base["sim"]["corner-mart"].pop("removal_check")
    assert sim == base["sim"], "the slice scripts are recall_1's, without Corner Mart's removal check"


def test_profile_counts(scn):
    truth = scn["sim_truth"]
    assert len(truth) == 36
    assert Counter(e["profile"] for e in truth.values()) == PROFILES
    assert truth["queen-st-grocer"]["profile"] == "forwarder"
    assert truth["lakeside-foods"]["profile"] == "straight"
    tier3 = {d["party_id"] for d in scn["directory"] if not d.get("slice") and d["tier"] == 3}
    simulated = {slugify(a["name"]) for a in accounts(scn)} | {"queen-st-grocer", "lakeside-foods"} | tier3
    assert set(truth) == simulated
    assert not {"harbour-market", "corner-mart"} & set(truth), "the two real phones have no profile"
    assert all({"name", "type", "tier", "upstream", "profile", "description", "truth", "params"} <= set(e)
               for e in truth.values())
    assert all(e["type"] == "store" for e in truth.values())


def test_order_systems_list_21_accounts_that_add_up(scn):
    assert set(scn["order_systems"]) == {"north", "east"}
    for order_system, prefix, total, n in (("north", "N", 1300, 12), ("east", "E", 860, 9)):
        data = scn["order_systems"][order_system]
        assert data["batch"] == "B-4471" and data["vendor"]
        rows = data["accounts"]
        assert len(rows) == n and sum(a["qty_shipped"] for a in rows) == total
        assert [a["account"] for a in rows] == [f"{prefix}-{i:03d}" for i in range(1, n + 1)]
        for a in rows:
            assert set(a) == {"account", "name", "phone", "qty_shipped", "qty_in_stock"}
            entry = scn["sim_truth"][slugify(a["name"])]
            assert (entry["type"], entry["tier"], entry["upstream"]) == ("store", 2, f"dist-{order_system}")
            assert entry["truth"]["received"] == a["qty_shipped"]
            assert entry["truth"]["held"] == a["qty_in_stock"], "in stock at the start: the tins on its shelf"
    assert len(accounts(scn)) == 21


def test_summit_foods_is_an_east_account_with_its_own_copy(scn):
    s = scn["sim_truth"]["summit-foods"]
    assert (s["tier"], s["upstream"], s["profile"], s["channel"], s["truth"]["received"]) == (
        2, "dist-east", "own_copy", "agent", 120)
    row = next(a for a in scn["order_systems"]["east"]["accounts"] if a["name"] == "Summit Foods")
    assert row["qty_shipped"] == 120
    entry = next(d for d in scn["directory"] if d["party_id"] == "summit-foods")
    assert (entry["channel"], entry["link_id"], entry["chain_stores"]) == ("agent", "ln-summit", 6)
    reports = s["params"]["reports"]
    assert [r["seq"] for r in reports] == list(range(1, len(reports) + 1))
    assert reports[0]["buckets"]["unresolved"] == 120 and 48 <= reports[0]["at"] < 72, "day 2"
    for r in reports:
        assert sum(r["buckets"].values()) == 120 and r["units_received"] == 120
        ev, b = r["evidence"], r["buckets"]
        assert (b["sold"], b["held"], b["on_shelf"]) == (ev["sold_by_till"], ev["held_by_system"],
                                                        ev["system_on_shelf"])
        assert r["state"] == ("closed" if not (b["on_shelf"] or b["unresolved"]) else "open")
    closed = [r for r in reports if r["state"] == "closed"]
    assert closed and 216 <= closed[0]["at"] < 240, "Summit closes on day 9"
    assert any(r["buckets"]["on_shelf"] == 9 and 120 <= r["at"] < 144 for r in reports), "day 5: 9 on a shelf"
    final = closed[-1]["buckets"]
    assert {k: final[k] for k in BUCKETS} == {k: s["truth"][k] for k in BUCKETS}
    assert set(final) == {"sold", "on_shelf", "held", "returned", "unresolved"}
    assert not any(k in json.dumps(reports) for k in ("phone", "+1")), "the buyer list never leaves the store"


def test_every_party_and_the_whole_batch_conserve(scn):
    shipped = {s["party_id"]: s["shipped_in"] for s in scn["parties"]}
    for sid, entry in scn["sim_truth"].items():
        t = entry["truth"]
        inflow = t["received"] if entry["tier"] > 1 else shipped[sid]
        assert inflow == outflow(t), sid
        assert all(isinstance(t[b], int) and t[b] >= 0 for b in ("sold", "held", "returned")), sid
        assert t.get("on_shelf", 0) == 0, "every simulated store ends with its tins pulled"
    harbour = scn["sim"]["harbour-market"]["initial"][-1]["claim"]
    corner = scn["sim"]["corner-mart"]["initial"][-1]["claim"]
    assert sum(harbour.get(b, 0) for b in BUCKETS) == shipped["harbour-market"]
    located = sum(sum(e["truth"].get(b, 0) for b in BUCKETS) for e in scn["sim_truth"].values())
    located += sum(harbour.get(b, 0) for b in BUCKETS) + sum(corner.get(b, 0) for b in BUCKETS)
    assert located == 2400, "every tin of the batch ends sold, on a shelf, held or returned somewhere"
    sent_by_dists = sum(e["truth"]["received"] for e in scn["sim_truth"].values() if e["tier"] == 2)
    assert sent_by_dists == shipped["dist-north"] + shipped["dist-east"], "the distributors forwarded everything"


def test_the_graph_one_three_hop_chain_and_short_receivers(scn):
    truth, directory = scn["sim_truth"], scn["directory"]
    named = Counter()
    for sid, entry in truth.items():
        for f in entry["truth"]["forwards"]:
            named[f["to_party_id"]] += 1
            assert resolve_party(f["to_name"], directory)[0] == f["to_party_id"], f
            if f["to_party_id"] in truth:
                assert truth[f["to_party_id"]]["truth"]["received"] == f["qty"]
                assert truth[f["to_party_id"]]["upstream"] == sid
    tier3 = [d for d in directory if d["tier"] == 3]
    assert {d["party_id"]: named[d["party_id"]] for d in tier3} == {d["party_id"]: 1 for d in tier3}
    assert len(tier3) == 14 and all(d["type"] == "store" for d in tier3)

    upstream = {sid: e["upstream"] for sid, e in truth.items()} | {"corner-mart": "queen-st-grocer"}

    def hops(sid):
        return 0 if upstream.get(sid) is None else 1 + hops(upstream[sid])
    depth = {sid: hops(sid) for sid in upstream}
    assert [sid for sid, n in depth.items() if n == 3] == ["willow-creek-mini-mart"]
    assert max(depth.values()) == 3
    chain = ["willow-creek-mini-mart"]
    while upstream.get(chain[-1]):
        chain.append(upstream[chain[-1]])
    assert chain[::-1] == ["dist-north", "birchmount-grocery", "juniper-lane-grocery", "willow-creek-mini-mart"]

    singles = [e for e in truth.values() if len(e["truth"]["forwards"]) == 1]
    several = [e for e in truth.values() if len(e["truth"]["forwards"]) > 1]
    assert singles and several, "both forward shapes are exercised"

    shorts = {sid: e for sid, e in truth.items() if e["profile"] == "short_receiver"}
    assert len(shorts) == 2
    for sid, e in shorts.items():
        sender = truth[e["params"]["sender"]]
        assert e["params"]["sender"] == e["upstream"] and sender["profile"] == "forwarder"
        (lie,) = [m for m in sender["params"]["misreport"] if m["to_party_id"] == sid]
        sent = next(f["qty"] for f in sender["truth"]["forwards"] if f["to_party_id"] == sid)
        assert lie["extra"] == e["params"]["short_by"] > 0
        assert e["truth"]["received"] == sent + lie["extra"] - e["params"]["short_by"]
        assert sender["truth"]["held"] >= lie["extra"], "the first claim moves the extra tins off its shelf count"

    (unreachable,) = [e for e in truth.values() if e["profile"] == "unreachable"]
    assert unreachable["tier"] == 2 and unreachable["upstream"].startswith("dist-")
    assert unreachable["params"]["rep_reports_at"] == 201.0


def test_every_store_follows_a_removal_script(scn):
    for sid, e in scn["sim_truth"].items():
        if e["tier"] == 1 or e["profile"] == "own_copy":
            assert "removal" not in e["truth"], sid
            continue
        removal = e["truth"]["removal"]
        assert 4.0 <= removal["after_h"] <= 12.0 and removal["batch"] == "B-4471", sid
        assert e["truth"]["held"] > 0, f"{sid} has tins on its shelf to pull"


def test_profile_parameters(scn):
    for sid, e in scn["sim_truth"].items():
        p, t = e["params"], e["truth"]
        if e["profile"] == "no_answer":
            assert p.get("answer_on_attempt") == 3 or len(p.get("answer_hours") or []) == 2, sid
            if p.get("answer_hours"):
                start, end = p["answer_hours"]
                assert 0 <= start < end <= 24 and p["form_after_hours"] > 0, sid
        if e["profile"] == "callback":
            assert 7 <= p["callback_hour"] <= 18
        if e["profile"] == "over_reporter":
            assert p["over_report_by"] > 0
        if e["profile"] == "reviser":
            assert p["revise"]["after_hours"] == 48.0 and 1 <= p["revise"]["delta"] <= t["held"]
    variants = Counter("hours" if e["params"].get("answer_hours") else "attempt"
                       for e in scn["sim_truth"].values() if e["profile"] == "no_answer")
    assert variants["hours"] >= 1 and variants["attempt"] >= 1
    assert any(e["truth"]["other_batches"].get("B-4480") for e in scn["sim_truth"].values())


def test_the_14_connected_stores(scn):
    stores = scn["connected_stores"]
    assert len(stores) == 14 and len({s["store_system_id"] for s in stores}) == 14
    assert sorted(s["name"] for s in stores if s["complaint"]) == ["Bayview Corner Store", "Maple Court Market",
                                                                   "Spadina Fresh"]
    (page,) = [s for s in stores if s["page"]]
    assert (page["party_id"], page["store_system_id"]) == ("corner-mart", "ss-corner-mart")
    assert all(s["party_id"] in scn["sim_truth"] for s in stores if not s["page"])


def test_phones_are_fictional_unique_and_new(scn):
    new = [a["phone"] for a in accounts(scn)] + [d["phone"] for d in scn["directory"]
                                                 if not d.get("slice") and d["tier"] == 3]
    slice_phones = [e.get("sim_phone") or e["phone"] for e in [*scn["parties"], *scn["directory"]] if e.get("slice")]
    assert all(FICTIONAL.match(p) for p in new + slice_phones)
    assert len(set(new + slice_phones)) == len(new) + len(slice_phones) == 38
    assert all(0 <= int(p[-4:]) - 100 <= 99 for p in new + slice_phones), "555-0100 to 555-0199"
    assert len({p[2:5] for p in new}) >= 4, "several area codes"
    assert not set(new) & gen.used_phones(), "no number of another scenario"
    assert not any(s.get("phone") for s in scn["parties"] if s.get("channel") == "voice"), \
        "real numbers come from the environment"


def test_schedule_and_expected(scn):
    assert scn["schedule"] == {"sim_start": 9.0, "seconds_per_sim_hour": 30, "hold_slice_in_demo": True,
                               "order_system_change_at": {"north": 81.5, "east": 82.0}, "rep_report_at": 201.0,
                               "days": 10}
    assert scn["expected"]["final"] == {"open_units": 40, "disputed_units": 15, "on_shelf": 25, "not_located": 0,
                                        "parties": 40, "at_start": 5,
                                        "headline": "NOT ALL CLEAR: 40 tins open (25 still on a shelf, 15 disputed)"}
    assert scn["expected"]["parties_by_tier"] == {"1": 5, "2": 21, "3": 14}
    assert scn["expected"]["profiles"] == PROFILES


# ---------------------------------------------------------------- the checks refuse broken scenarios

def broken(scn, change):
    copy_ = copy.deepcopy(scn)
    change(copy_)
    with pytest.raises(gen.ScenarioInvalid) as info:
        gen.check(copy_)
    return str(info.value)


def test_check_refuses_a_scenario_that_breaks_an_invariant(scn):
    gen.check(scn)
    assert "does not conserve" in broken(scn, lambda s: s["sim_truth"]["guildwood-grocer"]["truth"].update(
        held=s["sim_truth"]["guildwood-grocer"]["truth"]["held"] + 1))
    assert "profile counts" in broken(scn, lambda s: s["sim_truth"]["guildwood-grocer"].update(profile="callback"))
    assert "fictional" in broken(scn, lambda s: s["order_systems"]["east"]["accounts"][0].update(phone="+14165551234"))
    assert "short_by" in broken(scn, lambda s: s["sim_truth"]["larkspur-market"]["params"].update(short_by=1))
    assert "order system north" in broken(scn, lambda s: s["order_systems"]["north"]["accounts"][0].update(
        qty_shipped=1))
    assert "three hops" in broken(scn, lambda s: s["sim_truth"]["willow-creek-mini-mart"].update(
        upstream="birchmount-grocery"))
    assert "recall_1's" in broken(scn, lambda s: s["sim"]["lakeside-foods"]["initial"][0].update(say="hi"))
    assert "removal script" in broken(scn, lambda s: s["sim_truth"]["maple-court-market"]["truth"].pop("removal"))
    assert "on the shelf" in broken(scn, lambda s: s["sim_truth"]["bloor-foods"]["truth"].update(
        on_shelf=1, held=s["sim_truth"]["bloor-foods"]["truth"]["held"] - 1))
    assert "connected stores" in broken(scn, lambda s: s["connected_stores"].pop())
    assert "complaints" in broken(scn, lambda s: next(c for c in s["connected_stores"] if not c["complaint"]
                                                      and not c["page"]).update(complaint=True))
    assert "add up to 120" in broken(scn, lambda s: s["sim_truth"]["summit-foods"]["params"]["reports"][3][
        "buckets"].update(held=1))
    assert "evidence" in broken(scn, lambda s: s["sim_truth"]["summit-foods"]["params"]["reports"][5][
        "evidence"].update(held_by_system=0))
    assert "closed with tins open" in broken(scn, lambda s: s["sim_truth"]["summit-foods"]["params"]["reports"][2]
                                             .update(state="closed"))


def test_main_writes_only_a_checked_scenario(scn, tmp_path, monkeypatch):
    out = tmp_path / "recall_long.json"
    assert gen.main(["--seed", str(SEED), "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == scn
    bad = tmp_path / "bad.json"
    monkeypatch.setattr(gen, "build", lambda seed: {**copy.deepcopy(scn), "parties": scn["parties"][:3]})
    with pytest.raises(gen.ScenarioInvalid):
        gen.main(["--out", str(bad)])
    assert not bad.exists()


# ---------------------------------------------------------------- it starts

def test_start_recall_accepts_the_long_run(client, db, assert_balance):
    out = start_recall(client, db, load_scenario("recall_long"), all_sim=True, env={})
    assert out["recall_id"] == "RCL-2026-020"
    parties = {s["party_id"]: s for s in db.parties.find({"recall_id": "RCL-2026-020"})}
    assert set(parties) == {"harbour-market", "queen-st-grocer", "lakeside-foods", "dist-north", "dist-east"}
    assert (parties["dist-north"]["channel"], parties["dist-north"]["phone"], parties["dist-north"]["order_system"]) == \
        ("order_system", None, "north")
    assert parties["harbour-market"]["channel"] == "sim" and parties["harbour-market"]["slice"] is True
    assert parties["harbour-market"]["batch_code"] == "B-4471", "the batch code is copied onto each party"
    recall = db.recalls.find_one({"_id": "RCL-2026-020"})
    assert len(recall["directory"]) == 15 and recall["open_units"] == 2400
    run = get_run(db, "RCL-2026-020")
    assert run["parties_at_start"] == 5 and run["schedule"]["rep_report_at"] == 201.0
    assert run["events"]["order_system_change_at"] == {"north": 81.5, "east": 82.0}
    assert run["clock"]["seconds_per_sim_hour"] == 30.0 and run["clock"]["sim_start"] == 9.0
    assert_balance("RCL-2026-020")
