"""scenarios/generate.py and the recall_long.json it writes.

The invariants are re-checked here by independent arithmetic, not by calling
generate.check(): the profile counts, the order system totals, conservation for
every party and for the batch, the one chain three hops below the shipment
records, short receivers downstream of a forwarder that corrects itself, the
stores' fix scripts, the 24 connected tins, and fictional phone
numbers. check() itself is tested on broken copies.
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
BUCKETS = ("held", "on_shelf", "held", "returned")
FICTIONAL = re.compile(r"^\+1(416|647|437|905|289|365)55501\d\d$")
PROFILES = {"straight": 14, "no_answer": 5, "callback": 3, "over_reporter": 3, "reviser": 2, "forwarder": 6,
            "short_receiver": 2, "unreachable": 1}


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


def test_recall_and_the_shipment_list(scn):
    r = scn["recall"]
    assert (r["_id"], r["batch_code"], r["unit"], r["units_total"]) == ("RCL-2026-020", "B-4471", "tin", 2400)
    assert r["batch_spoken"] and r["supplier"] and r["product"] and r["hazard"] and r["checks"]
    assert (r["model"], r["affected_batches"], r["action"]) == ("B-4471", ["2.3", "2.4"], "2.5")
    assert not any(v in r["hazard"] for v in r["affected_batches"]), "the hazard reaches the agent"
    parties = {s["party_id"]: s for s in scn["parties"]}
    assert list(parties) == ["harbour-market", "queen-st-grocer", "lakeside-foods", "dist-north", "dist-east"]
    assert all(s["tier"] == 1 for s in parties.values())
    assert [s["type"] for s in parties.values()] == ["store"] * 3 + ["distributor"] * 2
    assert sum(s["shipped_in"] for s in parties.values()) == 2400
    for sid, name, order_system, qty in (("dist-north", "Distributor North", "north", 1300),
                                   ("dist-east", "Distributor East", "east", 860)):
        d = parties[sid]
        assert (d["name"], d["channel"], d["order_system"], d["shipped_in"]) == (name, "order_system", order_system, qty)
        assert d["profile"]
    base = load_scenario("recall_1")
    for s in base["parties"]:
        assert parties[s["party_id"]] == s and parties[s["party_id"]]["slice"] is True
    corner = scn["directory"][0]
    assert corner == base["directory"][0]
    assert (corner["type"], corner["tier"], corner["phone_env"], corner["slice"]) == ("owner", 3, "PHONE_CORNER", True)
    base["sim"]["corner-mart"].pop("removal_check")
    assert scn["sim"] == base["sim"], "the slice scripts are recall_1's, without Corner Mart' fix"


def test_profile_counts(scn):
    truth = scn["sim_truth"]
    assert len(truth) == 36
    assert Counter(e["profile"] for e in truth.values()) == PROFILES
    assert truth["queen-st-grocer"]["profile"] == "forwarder"
    assert truth["lakeside-foods"]["profile"] == "straight"
    simulated = ({slugify(a["name"]) for a in accounts(scn)} | {"queen-st-grocer", "lakeside-foods"}
                 | {d["party_id"] for d in scn["directory"] if not d.get("slice")})
    assert set(truth) == simulated
    assert not {"harbour-market", "corner-mart"} & set(truth), "the two real phones have no profile"
    assert all({"name", "type", "tier", "upstream", "profile", "description", "truth", "params"} <= set(e)
               for e in truth.values())


def test_order_systems_list_21_accounts_that_add_up(scn):
    assert set(scn["order_systems"]) == {"north", "east"}
    for order_system, prefix, total, n in (("north", "N", 1300, 12), ("east", "E", 860, 9)):
        data = scn["order_systems"][order_system]
        assert (data["batch"], data["vendor"]) == ("B-4471", "Kestrel OrderDesk")
        rows = data["accounts"]
        assert len(rows) == n and sum(a["qty_shipped"] for a in rows) == total
        assert [a["account"] for a in rows] == [f"{prefix}-{i:03d}" for i in range(1, n + 1)]
        for a in rows:
            assert set(a) == {"account", "name", "phone", "qty_shipped", "qty_in_stock"}
            entry = scn["sim_truth"][slugify(a["name"])]
            assert (entry["type"], entry["tier"], entry["upstream"]) == ("store", 2, f"dist-{order_system}")
            assert entry["truth"]["received"] == a["qty_shipped"]
            assert entry["truth"]["held"] == a["qty_in_stock"]
    assert len(accounts(scn)) == 21


def test_every_party_and_the_whole_batch_conserve(scn):
    shipped = {s["party_id"]: s["shipped_in"] for s in scn["parties"]}
    for sid, entry in scn["sim_truth"].items():
        t = entry["truth"]
        inflow = t["received"] if entry["tier"] > 1 else shipped[sid]
        assert inflow == outflow(t), sid
        assert all(isinstance(t[b], int) and t[b] >= 0 for b in BUCKETS), sid
    # The two real-phone parties' truths are their scripts' accepted answers.
    harbour = scn["sim"]["harbour-market"]["initial"][-1]["claim"]
    corner = scn["sim"]["corner-mart"]["initial"][-1]["claim"]
    assert sum(harbour.get(b, 0) for b in BUCKETS) == shipped["harbour-market"]
    held = sum(sum(e["truth"][b] for b in BUCKETS) for e in scn["sim_truth"].values())
    held += sum(harbour.get(b, 0) for b in BUCKETS) + sum(corner.get(b, 0) for b in BUCKETS)
    assert held == 2400, "every tin of the batch ends in stock, on_shelf, fixed or returned somewhere"
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
    assert {d["party_id"]: named[d["party_id"]] for d in directory} == {d["party_id"]: 1 for d in directory}
    assert len(directory) == 14 and all(d["tier"] == 3 for d in directory)
    assert Counter(d["type"] for d in directory) == {"owner": 13, "store": 1}

    upstream = {sid: e["upstream"] for sid, e in truth.items()} | {"corner-mart": "queen-st-grocer"}

    def hops(sid):
        return 0 if upstream.get(sid) is None else 1 + hops(upstream[sid])
    depth = {sid: hops(sid) for sid in upstream}
    assert [sid for sid, n in depth.items() if n == 3] == ["willow-creek-convenience"]
    assert max(depth.values()) == 3
    chain = ["willow-creek-convenience"]
    while upstream.get(chain[-1]):
        chain.append(upstream[chain[-1]])
    assert chain[::-1] == ["dist-north", "birchmount-fresh-market", "juniper-lane-variety", "willow-creek-convenience"]

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
        assert sender["truth"]["held"] >= lie["extra"], "the first claim moves the extra tins out of stock"

    (unreachable,) = [e for e in truth.values() if e["profile"] == "unreachable"]
    assert unreachable["tier"] == 2 and unreachable["upstream"].startswith("dist-")
    assert unreachable["params"]["rep_reports_at"] == 201.0


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


def test_phones_are_fictional_unique_and_new(scn):
    new = [a["phone"] for a in accounts(scn)] + [d["phone"] for d in scn["directory"] if not d.get("slice")]
    slice_phones = [e.get("sim_phone") or e["phone"] for e in [*scn["parties"], *scn["directory"]] if e.get("slice")]
    assert all(FICTIONAL.match(p) for p in new + slice_phones)
    assert len(set(new + slice_phones)) == len(new) + len(slice_phones) == 38
    assert len({p[2:5] for p in new}) >= 4, "several area codes"
    assert not set(new) & gen.used_phones(), "no number of another scenario"
    assert not any(s.get("phone") for s in scn["parties"] if s.get("channel") == "voice"), \
        "real numbers come from the environment"


def test_schedule_and_expected(scn):
    assert scn["schedule"] == {"sim_start": 9.0, "seconds_per_sim_hour": 30, "hold_slice_in_demo": True,
                               "order_system_change_at": {"north": 81.5, "east": 82.0}, "rep_report_at": 201.0,
                               "days": 10}
    assert scn["expected"]["final"] == {"open_units": 40, "disputed_units": 15, "on_shelf": 25, "parties": 40,
                                        "at_start": 5}
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
    assert "does not conserve" in broken(scn, lambda s: s["sim_truth"]["guildwood-market"]["truth"].update(
        held=s["sim_truth"]["guildwood-market"]["truth"]["held"] + 1))
    assert "profile counts" in broken(scn, lambda s: s["sim_truth"]["guildwood-market"].update(profile="callback"))
    assert "fictional" in broken(scn, lambda s: s["order_systems"]["east"]["accounts"][0].update(phone="+14165551234"))
    assert "short_by" in broken(scn, lambda s: s["sim_truth"]["larkspur-corner-store"]["params"].update(short_by=1))
    assert "order system north" in broken(scn, lambda s: s["order_systems"]["north"]["accounts"][0].update(
        qty_shipped=1))
    assert "three hops" in broken(scn, lambda s: s["sim_truth"]["willow-creek-convenience"].update(
        upstream="birchmount-fresh-market"))
    assert "recall_1's" in broken(scn, lambda s: s["sim"]["lakeside-foods"]["initial"][0].update(say="hi"))
    assert "fix script" in broken(scn, lambda s: s["sim_truth"]["maple-court-market"]["truth"].pop("fix"))
    assert "installs at a store's site" in broken(
        scn, lambda s: s["sim_truth"]["bloor-foods"]["truth"].update(on_shelf=1, held=76))
    assert "connected tins" in broken(scn, lambda s: s["connected_panels"].pop())
    assert "signal" in broken(scn, lambda s: next(p for p in s["connected_panels"] if not p["signal"]).update(
        signal=True))


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
    recall = db.recalls.find_one({"_id": "RCL-2026-020"})
    assert len(recall["directory"]) == 14 and recall["open_units"] == 2400
    run = get_run(db, "RCL-2026-020")
    assert run["parties_at_start"] == 5 and run["schedule"]["rep_report_at"] == 201.0
    assert run["events"]["order_system_change_at"] == {"north": 81.5, "east": 82.0}
    assert run["clock"]["seconds_per_sim_hour"] == 30.0 and run["clock"]["sim_start"] == 9.0
    assert_balance("RCL-2026-020")


# ---------------------------------------------------------------- owners and connected tins

def test_every_building_owner_follows_a_fix_script(scn):
    owners = {sid: e for sid, e in scn["sim_truth"].items() if e["type"] == "store"}
    assert len(owners) == 12
    for sid, e in owners.items():
        t, fix = e["truth"], e["truth"]["fix"]
        assert t["on_shelf"] > 0 and t["held"] == 0, sid
        assert 9.0 + 48 <= fix["at"] <= 9.0 + 96, "two to four simulated days into the run"
        assert (fix["model"], fix["version_after"]) == ("B-4471", "2.5")
        assert fix["version_before"] in ("2.3", "2.4")
    assert all("fix" not in e["truth"] and e["truth"]["on_shelf"] == 0
               for e in scn["sim_truth"].values() if e["type"] == "store")


def test_the_24_connected_panels(scn):
    tins = scn["connected_panels"]
    assert len(tins) == 24 and len({p["serial"] for p in tins}) == 24
    assert all(re.match(r"^D7-4471-\d{4}$", p["serial"]) and p["model"] == "B-4471" for p in tins)
    corner = next(p for p in tins if p["party_id"] == "corner-mart")
    assert (corner["serial"], corner["site_label"], corner["sw_version"], corner["fixed_at"]) == (
        "D7-4471-0193", "Corner Mart, Tower 2", "2.4", None)
    signal = sorted(p["site_label"] for p in tins if p["signal"])
    assert signal == ["Bayview Grocer", "Maple Court Market", "Spadina Foods"]
    truth = scn["sim_truth"]
    per_owner = Counter(p["party_id"] for p in tins if p["party_id"] != "corner-mart")
    for sid, n in per_owner.items():
        assert truth[sid]["type"] == "store" and n <= truth[sid]["truth"]["on_shelf"]
        assert all(p["fixed_at"] == truth[sid]["truth"]["fix"]["at"] for p in tins if p["party_id"] == sid)
