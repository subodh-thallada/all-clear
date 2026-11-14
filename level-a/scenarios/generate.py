"""Generate scenarios/recall_long.json: the long run's 40 parties.

    python scenarios/generate.py --seed 20261114 [--out PATH]

Batch B-4471 of Maple Farm milk powder 800 g (invented), 2,400 tins, recalled
by Maple Farm Foods for a faulty lid seal. Five parties are on the shipment
records (tier 1): the three stores of the live slice (Harbour Market 40, Queen
St Grocer 120, Lakeside Foods 80, with their scripted answers) and two
distributors (North 1,300, East 860) that sold everything to 21 accounts
listed in their order systems (tier 2): 20 stores and Summit Foods, a
six-store chain with 120 tins that answers through its own copy of the agent.
Six forwarders (stores that passed tins to stores on no list) name 14 stores
(tier 3): Corner Mart from the slice and 13 more. One chain runs three hops
below the shipment records (Distributor North -> Birchmount Grocery ->
Juniper Lane Grocery -> Willow Creek Mini Mart).

The 36 simulated parties each have a behaviour profile, a ground truth that
conserves, and the profile's parameters (levela/providers/profiles.py plays
them). Every simulated store follows a removal script: its tins are reported
on the shelf at first and held `removal.after_h` (4 to 12) simulated hours
after it first hears of the recall, once staff pull them. Summit Foods'
parameters are its node's reports (days 2 to 9, ending closed).

Names and the shape of the graph are written out below; the seed only picks
the numbers (quantities, hours) and the fictional phone numbers. The
generator uses its own small PRNG (splitmix64) rather than `random`, so every
Python version writes the same file. check() refuses to write a scenario that
breaks any invariant the run depends on: conservation for every party and for
the whole batch, the profile counts, the order system totals, the one
three-hop chain, short receivers downstream of a forwarder that corrects
itself on follow-up, the removal scripts, Summit's reports, fictional phones
only. main() also writes scenarios/summit.json through WS5's
scenarios/summit.py (same seed) when that module exists.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # level-a/
if str(ROOT) not in sys.path:                          # run as a script from anywhere
    sys.path.insert(0, str(ROOT))

from levela.ledger.transfers import _norm, resolve_party, slugify  # noqa: E402

SEED = 20261114
HERE = Path(__file__).resolve().parent
OUT = HERE / "recall_long.json"
SUMMIT_OUT = HERE / "summit.json"
OTHER_SCENARIOS = (HERE / "recall_1.json", HERE / "recall_2.json")

RECALL_ID = "RCL-2026-020"
UNITS_TOTAL = 2400
BATCH = "B-4471"
SCHEDULE = {
    "sim_start": 9.0,
    "seconds_per_sim_hour": 30,
    "hold_slice_in_demo": True,
    "order_system_change_at": {"north": 81.5, "east": 82.0},
    "rep_report_at": 201.0,
    "days": 10,
}
ORDER_SYSTEM_VENDOR = "Northgate OrderDesk"
OTHER_BATCH = "B-4480"            # the batch the demo's question asks about
AREA_CODES = ("416", "647", "437", "905", "289", "365")
FICTIONAL_PHONE = r"^\+1(416|647|437|905|289|365)55501\d\d$"
# Staff pull a store's tins this many simulated hours after it hears of the recall.
REMOVAL_HOURS = (4.0, 12.0)
# The three stores whose tins drew the day-0 complaints (connected stores).
COMPLAINT_STORES = ("Maple Court Market", "Bayview Corner Store", "Spadina Fresh")
CONNECTED_STORES = 14

PROFILE_COUNTS = {"straight": 13, "no_answer": 5, "callback": 3, "over_reporter": 3, "reviser": 2,
                  "forwarder": 6, "short_receiver": 2, "unreachable": 1, "own_copy": 1}
SLICE_PROFILES = {"queen-st-grocer": "forwarder", "lakeside-foods": "straight"}
DISPUTED_SLICE_UNITS = 15       # Queen St says 40 went to Corner Mart, Corner Mart says 25
SLICE_ON_SHELF = 25             # Corner Mart: all 25 still on the shelf

SLICE_SCENARIO = HERE / "recall_1.json"
# Scripts the long run drops: Corner Mart's removal check (its 25 tins stay on the shelf).
DROPPED_SCRIPTS = {"corner-mart": ("removal_check",)}

DISTRIBUTORS = [
    {"party_id": "dist-north", "name": "Distributor North", "order_system": "north", "prefix": "N",
     "shipped_in": 1300, "delivery_note": "DN-48216",
     "profile": "Grocery distributor serving stores north and west of the city. Ships by the pallet every week. "
                "Holds and account lists go through its order system."},
    {"party_id": "dist-east", "name": "Distributor East", "order_system": "east", "prefix": "E",
     "shipped_in": 860, "delivery_note": "DN-48217",
     "profile": "Grocery distributor serving stores east of the city, out to Durham. Ships twice a month. Holds "
                "and account lists go through its order system."},
]

SUMMIT = {"name": "Summit Foods", "received": 120, "link": "ln-summit",
          "where": "A six-store grocery chain in Durham with its own depot and its own copy of the recall agent"}

# Distributor accounts (tier 2): (order system, name, profile, variant, where).
ACCOUNTS = [
    ("north", "Birchmount Grocery", "forwarder", None,
     "Grocery on Birchmount Rd in Scarborough with a stock room that supplies two smaller stores"),
    ("north", "Pinecrest Market", "forwarder", None,
     "Market on Pinecrest Ave in North York that passes spare stock to corner stores"),
    ("north", "Tallpine General Store", "unreachable", None,
     "General store on a rural route north of the city"),
    ("north", "Morrow Avenue Foods", "no_answer", "hours",
     "Small grocery on Morrow Ave in the Junction Triangle"),
    ("north", "Elmvale Grocer", "no_answer", "attempt",
     "Family grocer on Elmvale Rd in East York"),
    ("north", "Cedarbrae Fresh Market", "callback", None,
     "Fresh market in the Cedarbrae plaza in Scarborough"),
    ("north", "Ridgeway Foods", "over_reporter", None,
     "Grocery on Ridgeway Dr in Etobicoke"),
    ("north", "Glenholme Grocery", "reviser", None,
     "Family-run grocery on Glenholme Ave in Oakwood Village"),
    ("north", "Lambton Mills Market", "straight", None,
     "Market on Old Dundas St by the Humber River"),
    ("north", "Bloor Foods", "straight", None,
     "Grocery on Bloor St West with a busy till"),
    ("north", "Humber Bay Grocer", "straight", None,
     "Grocer serving the condo towers of Humber Bay"),
    ("north", "Silverthorn Market", "straight", None,
     "Market on Silverthorn Ave near Eglinton Ave West"),
    ("east", "Fernbrook Foods", "forwarder", None,
     "Grocery in a Pickering plaza that supplies three smaller stores"),
    ("east", "Annex Grocer", "forwarder", None,
     "Grocer in the Annex that passes stock to stores downtown"),
    ("east", "Brookbanks Market", "no_answer", "hours",
     "Small market on Brookbanks Dr in Don Mills"),
    ("east", "Thistledown Grocery", "callback", None,
     "Grocery in a strip plaza in Thistletown, Etobicoke"),
    ("east", "Cliffcrest Foods", "over_reporter", None,
     "Grocery on Kingston Rd in Cliffcrest"),
    ("east", "Wexford Market", "straight", None,
     "Market on Lawrence Ave East in Wexford"),
    ("east", "Guildwood Grocer", "straight", None,
     "Independent grocer in Guildwood Village"),
    ("east", "Orchard Park Foods", "straight", None,
     "Grocery on Orchard Park Blvd in Ajax"),
    ("east", SUMMIT["name"], "own_copy", None, SUMMIT["where"]),
]

# Stores on no list (tier 3): (name, profile, variant, sender name, where, aliases).
PARTIES = [
    ("Juniper Lane Grocery", "forwarder", None, "Birchmount Grocery",
     "Small grocery on Juniper Lane in Scarborough that passes tins to a mini mart",
     ["juniper lane grocery", "the juniper lane grocery", "juniper lane", "juniper grocery"]),
    ("Willow Creek Mini Mart", "straight", None, "Juniper Lane Grocery",
     "Mini mart by Willow Creek park in Scarborough",
     ["willow creek mini mart", "the willow creek mini mart", "willow creek", "willow creek mart"]),
    ("Bayview Corner Store", "no_answer", "hours", "Birchmount Grocery",
     "Corner store at the top of Bayview Ave",
     ["bayview corner store", "the bayview corner store", "bayview", "bay view corner store"]),
    ("Larkspur Market", "short_receiver", None, "Birchmount Grocery",
     "Neighbourhood market on Larkspur Ave",
     ["larkspur market", "the larkspur market", "larkspur", "larkspur mkt"]),
    ("Kingsway Variety", "callback", None, "Pinecrest Market",
     "Variety store on Bloor St West in the Kingsway",
     ["kingsway variety", "the kingsway variety", "kingsway", "kingsway variety store"]),
    ("Old Mill Grocer", "short_receiver", None, "Pinecrest Market",
     "Grocer near the Old Mill bridge",
     ["old mill grocer", "the old mill grocer", "old mill grocery", "oldmill grocer"]),
    ("Rosehill Foods", "over_reporter", None, "Pinecrest Market",
     "Co-operative food store on Rosehill Ave by the reservoir park",
     ["rosehill foods", "the rosehill foods", "rosehill", "rose hill foods"]),
    ("Harrow Street Market", "reviser", None, "Fernbrook Foods",
     "Market on Harrow St in Pickering village",
     ["harrow street market", "the harrow street market", "harrow st market", "harrow market"]),
    ("Clover Hill Variety", "no_answer", "attempt", "Fernbrook Foods",
     "Variety store at the Clover Hill crossroads",
     ["clover hill variety", "the clover hill variety", "clover hill", "cloverhill variety"]),
    ("Maple Court Market", "straight", None, "Fernbrook Foods",
     "Market on the ground floor of a residential court on Maple Ave in Pickering",
     ["maple court market", "the maple court market", "maple court", "maple ct market"]),
    ("Dundas Mini Mart", "straight", None, "Annex Grocer",
     "Mini mart on Dundas St West downtown",
     ["dundas mini mart", "the dundas mini mart", "dundas mart", "dundas st mini mart"]),
    ("Spadina Fresh", "straight", None, "Annex Grocer",
     "Fresh food store in a converted factory on Spadina Ave",
     ["spadina fresh", "the spadina fresh", "spadina", "spadina fresh market"]),
    ("Gable Street Co-op Grocery", "straight", None, "Annex Grocer",
     "Co-operative grocery on Gable St in Ajax",
     ["gable street co-op grocery", "the gable street co-op grocery", "gable street co-op",
      "gable st co-op grocery"]),
]

# What a forwarder calls a store when it is not the store's own name.
SAID_AS = {"Gable Street Co-op Grocery": "Gable Street Co-op", "Maple Court Market": "Maple Court"}
# The forwarder that passes its tins on gets the biggest share of its sender's forwards.
ONWARD_WEIGHT = 1.6

HABITS = {
    ("straight", None): "The manager answers the phone during opening hours and reads counts off the till.",
    ("no_answer", "hours"): "The manager is on the floor in the morning; the phone is only answered between "
                            "{start}:00 and {end}:00.",
    ("no_answer", "attempt"): "One person runs the store alone; the phone often rings out or is engaged and is "
                              "usually picked up only after a couple of tries.",
    ("callback", None): "The manager keeps the delivery slips in the office and asks to be called back the next "
                        "morning at {hour}:00.",
    ("over_reporter", None): "Counts are read off a shared stock sheet, and the first figure given is often from "
                             "the wrong line.",
    ("reviser", None): "Keeps careful paperwork and sends a correction when tins go back after a call.",
    ("forwarder", None): "Passes spare stock to smaller stores nearby, none of them on a distributor's list.",
    ("short_receiver", None): "Checks every delivery slip against the tins that actually arrived.",
    ("unreachable", None): "The line is unreliable and messages go unanswered; the distributor's sales rep visits "
                           "weekly and knows the stock.",
    ("own_copy", None): "Runs its own copy of the recall agent, which takes the sub-recall and reports counts; "
                        "its buyer list never leaves the chain.",
}

BUCKETS = ("sold", "on_shelf", "held", "returned")
REPORT_BUCKETS = ("sold", "on_shelf", "held", "returned", "unresolved")


class ScenarioInvalid(ValueError):
    """The generated scenario breaks an invariant; nothing is written."""


# ------------------------------------------------------------------ numbers

class Rng:
    """splitmix64: tiny, fast and identical on every Python version."""

    MASK = (1 << 64) - 1

    def __init__(self, seed: int):
        self.state = seed & self.MASK

    def _next(self) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) & self.MASK
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & self.MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & self.MASK
        return z ^ (z >> 31)

    def random(self) -> float:
        return (self._next() >> 11) / float(1 << 53)

    def uniform(self, a: float, b: float) -> float:
        return a + (b - a) * self.random()

    def randint(self, a: int, b: int) -> int:
        return a + self._next() % (b - a + 1)


def split(total: int, weights: list[float]) -> list[int]:
    """Integers proportional to `weights` that sum to exactly `total`
    (largest remainder; ties go to the earlier entry)."""
    scale = sum(weights)
    exact = [total * w / scale for w in weights]
    out = [int(x) for x in exact]
    order = sorted(range(len(exact)), key=lambda i: (-(exact[i] - out[i]), i))
    for i in order[:total - sum(out)]:
        out[i] += 1
    return out


def bucket_split(rng: Rng, kept: int) -> dict:
    """What a store did with the tins it kept, at the end of its removal
    script: some sold before the recall, the rest held once staff pulled
    them, and sometimes a few returned. Nothing is on the shelf at the end;
    `held` is what stands on the shelf before the pull."""
    weights = [rng.uniform(0.3, 0.6), 0.0, rng.uniform(0.35, 0.65),
               rng.uniform(0.03, 0.1) if rng.random() < 0.4 else 0.0]
    out = dict(zip(BUCKETS, split(kept, weights)))
    if out["held"] == 0 and out["sold"] > 0:           # every store has tins to pull
        out["sold"] -= 1
        out["held"] += 1
    return out


# ------------------------------------------------------------------ build

def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def used_phones() -> set[str]:
    """Numbers already in the other scenarios."""
    phones: set = set()
    for path in OTHER_SCENARIOS:
        if not path.exists():
            continue
        scn = _load(path)
        for entry in [*scn.get("parties", []), *scn.get("directory", [])]:
            phones.update({entry.get("phone"), entry.get("sim_phone")})
    return {p for p in phones if p}


def _slice_phones() -> set[str]:
    base = _load(SLICE_SCENARIO)
    return {e.get("sim_phone") or e.get("phone") for e in [*base["parties"], *base["directory"]]}


def phone_book(rng: Rng, n: int) -> list[str]:
    """n distinct numbers in the fictional 555-01xx block, cycling area codes."""
    taken, out = used_phones() | _slice_phones(), []
    for i in range(n):
        area = AREA_CODES[i % len(AREA_CODES)]
        while True:
            number = f"+1{area}55501{rng.randint(0, 99):02d}"
            if number not in taken:
                break
        taken.add(number)
        out.append(number)
    return out


def last_claim(replies: list) -> dict:
    return dict(replies[-1]["claim"])


def slice_truths(sim: dict) -> dict:
    """The slice parties' truths at the end of the long run, read from their
    scripts (the last answer each gives; Corner Mart's tins stay on the shelf)."""
    return {"harbour-market": last_claim(sim["harbour-market"]["initial"]),
            "queen-st-grocer": last_claim(sim["queen-st-grocer"]["dispute_followup"]),
            "lakeside-foods": last_claim(sim["lakeside-foods"]["initial"]),
            "corner-mart": last_claim(sim["corner-mart"]["initial"])}


def long_run_sim(base: dict) -> dict:
    """recall_1's scripts without Corner Mart's removal check: in the long
    run its tins stay on the shelf, so a removal check replays its first
    answer and the slice ends with 25 tins on the shelf."""
    sim = json.loads(json.dumps(base["sim"]))
    for party_id, keys in DROPPED_SCRIPTS.items():
        for key in keys:
            sim.get(party_id, {}).pop(key, None)
    return sim


def build(seed: int = SEED) -> dict:
    """The whole scenario as a dict (deterministic for a seed and recall_1.json)."""
    rng = Rng(seed)
    base = _load(SLICE_SCENARIO)
    recall = {**base["recall"], "_id": RECALL_ID, "units_total": UNITS_TOTAL}
    slice_parties = json.loads(json.dumps(base["parties"]))
    corner = json.loads(json.dumps(base["directory"][0]))
    sim = long_run_sim(base)
    truths = slice_truths(sim)

    accounts = _accounts_shipped(rng)
    parties = _flows(rng, accounts)
    nodes = {**accounts, **parties}
    _removal_scripts(rng, nodes)
    params = _params(rng, nodes)
    for node, phone in zip(nodes.values(), phone_book(rng, len(nodes))):
        node["phone"] = phone
    batches = _other_batches(rng, [*SLICE_PROFILES, *nodes])

    order_systems = {d["order_system"]: {"batch": recall["batch_code"], "vendor": ORDER_SYSTEM_VENDOR,
                                         "accounts": []} for d in DISTRIBUTORS}
    counters = Counter()
    for node in accounts.values():
        dist = _distributor(node["order_system"])
        counters[node["order_system"]] += 1
        order_systems[node["order_system"]]["accounts"].append({
            "account": f"{dist['prefix']}-{counters[node['order_system']]:03d}", "name": node["name"],
            "phone": node["phone"], "qty_shipped": node["truth"]["received"],
            "qty_in_stock": node["truth"]["held"]})

    directory = [corner] + [{
        "party_id": sid, "name": node["name"], "aliases": node["aliases"], "type": "store", "tier": 3,
        "channel": "sim", "phone": node["phone"], "profile": _profile_text(node, params[sid]),
    } for sid, node in parties.items()]
    summit_id = slugify(SUMMIT["name"])
    directory.append({"party_id": summit_id, "name": SUMMIT["name"],
                      "aliases": ["summit foods", "summit", "the summit foods", "summit food"],
                      "type": "store", "tier": 2, "channel": "agent", "phone": accounts[summit_id]["phone"],
                      "link_id": SUMMIT["link"], "chain_stores": 6,
                      "profile": _profile_text(accounts[summit_id], params[summit_id])})

    sim_truth = {}
    for sid, profile in SLICE_PROFILES.items():
        party = next(s for s in slice_parties if s["party_id"] == sid)
        sim_truth[sid] = {"name": party["name"], "type": party["type"], "tier": 1, "upstream": None,
                          "profile": profile, "description": party["profile"],
                          "truth": {**_truth_from_claim(truths[sid], directory), "other_batches": batches[sid]},
                          "params": {}}
    for sid, node in nodes.items():
        sim_truth[sid] = {"name": node["name"], "type": "store", "tier": node["tier"],
                          "upstream": node["upstream"], "profile": node["profile"],
                          "description": _profile_text(node, params[sid]),
                          "truth": {**node["truth"], "other_batches": batches[sid]}, "params": params[sid]}
        if node["profile"] == "own_copy":
            sim_truth[sid]["channel"] = "agent"

    shipment_parties = slice_parties + [{
        "party_id": d["party_id"], "name": d["name"], "type": "distributor", "tier": 1, "channel": "order_system",
        "order_system": d["order_system"], "shipped_in": d["shipped_in"], "delivery_note": d["delivery_note"],
        "profile": d["profile"],
    } for d in DISTRIBUTORS]
    total = len(shipment_parties) + len(nodes) + 1

    return {
        "scenario_id": "recall_long",
        "synthetic": True,
        "about": "The long run. Batch B-4471 of Maple Farm milk powder 800 g (invented), 2,400 tins, 40 parties "
                 "over 10 simulated days: 5 on the shipment records, 21 accounts found in the distributors' order "
                 "systems (20 stores and Summit Foods, a six-store chain with its own copy of the agent), 14 "
                 "stores named by stores that passed tins on (Corner Mart from the slice and 13 more). 36 are "
                 "simulated; Harbour Market and Corner Mart answer real phones in the demo. Every simulated "
                 "store's tins are on the shelf at first and held 4 to 12 simulated hours after it hears of the "
                 "recall. Ends with only the slice's tins open (15 disputed, 25 on the shelf at Corner Mart), so "
                 "the recall cannot close.",
        "seed": seed,
        "recall": recall,
        "parties": shipment_parties,
        "directory": directory,
        "order_systems": order_systems,
        "connected_stores": _connected_stores(rng, nodes),
        "sim": sim,
        "sim_truth": sim_truth,
        "schedule": json.loads(json.dumps(SCHEDULE)),
        "expected": {
            "note": "Every simulated party's truth conserves and the batch adds up to units_total (checked by "
                    "generate.py before writing). final: what the run ends with when the slice's follow-ups are "
                    "manual (demo): the Queen St Grocer -> Corner Mart dispute stays open and Corner Mart's 25 "
                    "tins stay on the shelf.",
            "final": {"open_units": DISPUTED_SLICE_UNITS + SLICE_ON_SHELF, "disputed_units": DISPUTED_SLICE_UNITS,
                      "on_shelf": SLICE_ON_SHELF, "not_located": 0, "parties": total,
                      "at_start": len(shipment_parties),
                      "headline": f"NOT ALL CLEAR: {DISPUTED_SLICE_UNITS + SLICE_ON_SHELF} tins open "
                                  f"({SLICE_ON_SHELF} still on a shelf, {DISPUTED_SLICE_UNITS} disputed)"},
            "parties_by_tier": {"1": len(shipment_parties), "2": len(accounts), "3": len(parties) + 1},
            "profiles": dict(PROFILE_COUNTS),
        },
    }


def _distributor(order_system: str) -> dict:
    return next(d for d in DISTRIBUTORS if d["order_system"] == order_system)


def _truth_from_claim(claim: dict, directory: list) -> dict:
    """A scripted claim as a truth: buckets plus its one forward, if any."""
    truth = {b: int(claim.get(b, 0)) for b in BUCKETS}
    if claim.get("batch_seen"):
        truth["batch"] = claim["batch_seen"]
    truth["forwards"] = []
    if claim.get("forwarded_qty"):
        to_id, _ = resolve_party(claim["forwarded_to_name"], directory)
        truth["forwards"].append({"to_party_id": to_id, "to_name": claim["forwarded_to_name"],
                                  "qty": int(claim["forwarded_qty"])})
    return truth


def _accounts_shipped(rng: Rng) -> dict:
    """Each distributor's tins over its accounts; forwarders get the big shares,
    Summit Foods exactly its 120."""
    out = {}
    for dist in DISTRIBUTORS:
        rows = [a for a in ACCOUNTS if a[0] == dist["order_system"]]
        fixed = sum(SUMMIT["received"] for a in rows if a[2] == "own_copy")
        free = [a for a in rows if a[2] != "own_copy"]
        weights = [1.8 if a[2] == "forwarder" else rng.uniform(0.7, 1.3) for a in free]
        qty = dict(zip((a[1] for a in free), split(dist["shipped_in"] - fixed, weights)))
        for order_system, name, profile, variant, where in rows:
            received = SUMMIT["received"] if profile == "own_copy" else qty[name]
            out[slugify(name)] = {"name": name, "order_system": order_system, "profile": profile,
                                  "variant": variant, "where": where, "tier": 2, "upstream": dist["party_id"],
                                  "truth": {"received": received}}
    return out


def _flows(rng: Rng, accounts: dict) -> dict:
    """Every account's and store's forwards and buckets, in table order (a store
    that forwards appears before the store it supplies). Returns the tier 3 stores."""
    parties = {slugify(s[0]): {"name": s[0], "profile": s[1], "variant": s[2], "sender": s[3], "where": s[4],
                               "aliases": s[5], "tier": 3, "upstream": slugify(s[3]), "truth": {}}
               for s in PARTIES}
    nodes = {**accounts, **parties}
    for sid, node in nodes.items():
        if "received" not in node["truth"]:
            node["truth"]["received"] = _received(nodes, sid)
        if node["profile"] == "own_copy":
            node["truth"].update(_summit_final())
            node["truth"]["forwards"] = []
            continue
        dests = [d for d, party in parties.items() if party["upstream"] == sid]
        forwards = []
        if dests:
            share = rng.uniform(0.5, 0.65) if node["tier"] == 2 else rng.uniform(0.35, 0.5)
            total = round(node["truth"]["received"] * share)
            weights = [ONWARD_WEIGHT if any(s["upstream"] == d for s in parties.values()) else rng.uniform(0.8, 1.2)
                       for d in dests]
            forwards = [{"to_party_id": d, "to_name": SAID_AS.get(parties[d]["name"], parties[d]["name"]), "qty": q}
                        for d, q in zip(dests, split(total, weights))]
        kept = node["truth"]["received"] - sum(f["qty"] for f in forwards)
        node["truth"].update(bucket_split(rng, kept))
        node["truth"]["forwards"] = forwards
    return parties


def _received(nodes: dict, sid: str) -> int:
    sender = nodes[nodes[sid]["upstream"]]
    return next(f["qty"] for f in sender["truth"]["forwards"] if f["to_party_id"] == sid)


def _removal_scripts(rng: Rng, nodes: dict) -> None:
    """Every simulated store's removal script: its tins stand on the shelf
    until staff pull them `after_h` simulated hours after it first hears of
    the recall; from then on they are held, the batch code is read from a tin
    and the till refuses the sale. Summit's own node runs its own removal."""
    for node in nodes.values():
        if node["profile"] == "own_copy":
            continue
        node["truth"]["removal"] = {"after_h": round(rng.uniform(*REMOVAL_HOURS), 1), "batch": BATCH}


def _summit_final() -> dict:
    """Summit's end state in our ledger: 31 sold, 67 held (18 at its depot, 49
    at its stores), 22 returned, none on a shelf."""
    return {"sold": 31, "on_shelf": 0, "held": 67, "returned": 22}


# Summit's report trajectory: (sold, on_shelf, held, returned, unresolved), one
# report every 12 simulated hours from day 2 11:00 to day 9 23:00.
SUMMIT_TRAJECTORY = [
    (0, 0, 0, 0, 120), (31, 47, 18, 0, 24), (31, 40, 25, 10, 14), (31, 30, 37, 16, 6),
    (31, 22, 45, 22, 0), (31, 15, 52, 22, 0), (31, 9, 58, 22, 0), (31, 9, 58, 22, 0),
    (31, 9, 58, 22, 0), (31, 7, 60, 22, 0), (31, 5, 62, 22, 0), (31, 3, 64, 22, 0),
    (31, 2, 65, 22, 0), (31, 1, 66, 22, 0), (31, 0, 67, 22, 0), (31, 0, 67, 22, 0),
]
SUMMIT_FIRST_REPORT_AT = 59.0          # day 2 11:00
SUMMIT_REPORT_EVERY_H = 12.0
SUMMIT_TOLD = (0, 0, 4, 8, 11, 13, 14, 14, 15, 15, 16, 16, 17, 17, 17, 17)


def summit_reports() -> list[dict]:
    """Summit's node's reports (spec "The report"), without the recall id,
    which the simulator fills in. The last two say closed."""
    out = []
    for i, (sold, shelf, held, returned, unresolved) in enumerate(SUMMIT_TRAJECTORY):
        reconciled = min(7, i // 2) if shelf or unresolved else 7
        identified = 19 if i else 0
        unreachable = 2 if i >= 12 else 0
        out.append({
            "link": SUMMIT["link"], "seq": i + 1, "at": SUMMIT_FIRST_REPORT_AT + SUMMIT_REPORT_EVERY_H * i,
            "units_received": SUMMIT["received"],
            "buckets": {"sold": sold, "on_shelf": shelf, "held": held, "returned": returned,
                        "unresolved": unresolved},
            "evidence": {"sold_by_till": sold, "held_by_system": held, "system_on_shelf": shelf},
            "buyers": {"identified": identified, "told": SUMMIT_TOLD[i], "unreachable": unreachable,
                       "anonymous_sales": 12 if i else 0},
            "parties": {"total": 7, "reconciled": reconciled, "open": 7 - reconciled},
            "state": "closed" if not (shelf or unresolved) else "open",
        })
    return out


def _params(rng: Rng, nodes: dict) -> dict:
    """Each profile's parameters. A short receiver's sender over-reports that
    one forward on its first call (taking the tins from its shelf count) and
    corrects it on the dispute follow-up."""
    params = {}
    for sid, node in nodes.items():
        profile, variant, truth = node["profile"], node["variant"], node["truth"]
        p: dict = {}
        if profile == "no_answer" and variant == "hours":
            start = 13 + rng.randint(0, 2)
            p = {"answer_hours": [start, start + 4], "form_after_hours": float(2 + rng.randint(0, 2))}
        elif profile == "no_answer":
            p = {"answer_on_attempt": 3, "form_after_hours": None}
        elif profile == "callback":
            p = {"callback_hour": 9 + rng.randint(0, 2)}
        elif profile == "over_reporter":
            p = {"over_report_by": 8 + rng.randint(0, 12)}
        elif profile == "reviser":
            p = {"revise": {"after_hours": 48.0, "delta": max(1, min(10, truth["held"] // 2))}}
        elif profile == "unreachable":
            p = {"rep_reports_at": SCHEDULE["rep_report_at"], "form_after_hours": None}
        elif profile == "own_copy":
            p = {"link": SUMMIT["link"], "reports": summit_reports()}
        params[sid] = p
    for sid, node in nodes.items():
        if node["profile"] != "short_receiver":
            continue
        sender_id = node["upstream"]
        extra = min(4 + rng.randint(0, 4), nodes[sender_id]["truth"]["held"])
        params[sid] = {"short_by": extra, "sender": sender_id}
        params[sender_id].setdefault("misreport", []).append({"to_party_id": sid, "extra": extra})
    return params


def _other_batches(rng: Rng, party_ids: list[str]) -> dict:
    """About a third of the stores also took delivery of batch B-4480 (the
    demo's question: "Did you also receive batch B-4480?")."""
    return {sid: ({OTHER_BATCH: 6 + rng.randint(0, 18)} if rng.random() < 0.35 else {}) for sid in party_ids}


def _profile_text(node: dict, params: dict) -> str:
    """The party's profile text (parties.profile; what memory embeds). Accounts
    have no directory entry, so theirs is sim_truth[...].description."""
    habit = HABITS[(node["profile"], node["variant"])]
    hours = params.get("answer_hours") or [0, 0]
    return f"{node['where']}. " + habit.format(start=hours[0], end=hours[1], hour=params.get("callback_hour", 9))


def _connected_stores(rng: Rng, nodes: dict) -> list[dict]:
    """The 14 connected stores (till and stock records that report to the
    recall): Corner Mart (the simulated store page), the three stores whose
    tins drew the day-0 complaints, and ten more simulated stores. Data for
    the store records workstream; the phone ledger does not read it."""
    by_name = {n["name"]: sid for sid, n in nodes.items()}
    chosen = [by_name[name] for name in COMPLAINT_STORES]
    others = sorted(sid for sid, n in nodes.items()
                    if n["profile"] not in ("own_copy", "unreachable", "forwarder") and sid not in chosen)
    while len(chosen) < CONNECTED_STORES - 1:
        sid = others[rng.randint(0, len(others) - 1)]
        if sid not in chosen:
            chosen.append(sid)
    stores = [{"store_system_id": "ss-corner-mart", "party_id": "corner-mart", "name": "Corner Mart",
               "complaint": False, "page": True}]
    stores += [{"store_system_id": f"ss-{sid}", "party_id": sid, "name": nodes[sid]["name"],
                "complaint": nodes[sid]["name"] in COMPLAINT_STORES, "page": False} for sid in sorted(chosen)]
    return stores


# ------------------------------------------------------------------ checks

def check(scn: dict) -> None:
    """Raise ScenarioInvalid listing every broken invariant."""
    errors: list[str] = []
    for rule in (_check_profiles, _check_order_systems, _check_conservation, _check_graph, _check_params,
                 _check_removal_scripts, _check_summit, _check_connected, _check_contacts, _check_slice,
                 _check_expected):
        try:
            errors.extend(rule(scn))
        except (KeyError, TypeError, ValueError, StopIteration, RecursionError) as exc:
            errors.append(f"{rule.__name__.removeprefix('_check_')}: the scenario is malformed ({exc!r})")
    if errors:
        raise ScenarioInvalid("recall_long is invalid:\n  " + "\n  ".join(errors))


def _accounts(scn: dict) -> list[dict]:
    return [a for order_system in scn["order_systems"].values() for a in order_system["accounts"]]


def _tier3_ids(scn: dict) -> list[str]:
    return [d["party_id"] for d in scn["directory"] if not d.get("slice") and d.get("tier") == 3]


def _check_profiles(scn: dict) -> list[str]:
    truth = scn["sim_truth"]
    expected_ids = {slugify(a["name"]) for a in _accounts(scn)} | set(SLICE_PROFILES) | set(_tier3_ids(scn))
    errors = []
    if set(truth) != expected_ids:
        errors.append(f"sim_truth covers {sorted(set(truth) ^ expected_ids)} wrongly")
    got = Counter(e["profile"] for e in truth.values())
    if dict(got) != PROFILE_COUNTS:
        errors.append(f"profile counts {dict(got)} != {PROFILE_COUNTS}")
    for sid, profile in SLICE_PROFILES.items():
        if truth.get(sid, {}).get("profile") != profile:
            errors.append(f"{sid} must be {profile}")
    if any(e["type"] != "store" for e in truth.values()):
        errors.append("every simulated party is a store")
    return errors


def _check_order_systems(scn: dict) -> list[str]:
    errors = []
    dists = {s["order_system"]: s for s in scn["parties"] if s.get("channel") == "order_system"}
    accounts = _accounts(scn)
    if len(accounts) != 21:
        errors.append(f"{len(accounts)} order system accounts, expected 21")
    for order_system, data in scn["order_systems"].items():
        if order_system not in dists:
            errors.append(f"order system {order_system} has no distributor on the shipment records")
            continue
        total = sum(a["qty_shipped"] for a in data["accounts"])
        if total != dists[order_system]["shipped_in"]:
            errors.append(f"order system {order_system} accounts ship {total}, distributor shipped "
                          f"{dists[order_system]['shipped_in']}")
        for a in data["accounts"]:
            entry = scn["sim_truth"].get(slugify(a["name"]), {})
            if entry.get("truth", {}).get("received") != a["qty_shipped"]:
                errors.append(f"{a['name']} received differs from the order system's qty_shipped")
            if entry.get("upstream") != dists[order_system]["party_id"]:
                errors.append(f"{a['name']} is not supplied by {dists[order_system]['party_id']}")
    if len({a["account"] for a in accounts}) != len(accounts):
        errors.append("order system account ids repeat")
    return errors


def _inflow(scn: dict, sid: str, entry: dict) -> int:
    if entry["tier"] == 1:
        return next(s["shipped_in"] for s in scn["parties"] if s["party_id"] == sid)
    return entry["truth"]["received"]


def _outflow(truth: dict) -> int:
    return sum(int(truth.get(b, 0)) for b in BUCKETS) + sum(f["qty"] for f in truth.get("forwards") or [])


def _check_conservation(scn: dict) -> list[str]:
    errors = []
    for sid, entry in scn["sim_truth"].items():
        t = entry["truth"]
        values = [t.get(b, 0) for b in BUCKETS] + [f["qty"] for f in t.get("forwards") or []]
        if any(not isinstance(v, int) or v < 0 for v in values):
            errors.append(f"{sid} has a count that is not a non-negative integer")
        if _inflow(scn, sid, entry) != _outflow(t):
            errors.append(f"{sid} does not conserve: in {_inflow(scn, sid, entry)}, out {_outflow(t)}")
        if t.get("on_shelf", 0):
            errors.append(f"{sid} ends with tins on the shelf; its removal script must pull them")
    truths = slice_truths(scn["sim"])
    located = sum(sum(int(e["truth"].get(b, 0)) for b in BUCKETS) for e in scn["sim_truth"].values())
    located += sum(int(truths[s].get(b, 0)) for s in ("harbour-market", "corner-mart") for b in BUCKETS)
    if located != scn["recall"]["units_total"]:
        errors.append(f"the batch does not add up: {located} tins located, {scn['recall']['units_total']} shipped")
    shipped = sum(s["shipped_in"] for s in scn["parties"])
    if shipped != scn["recall"]["units_total"]:
        errors.append(f"shipment records total {shipped}, units_total {scn['recall']['units_total']}")
    return errors


def _depths(scn: dict) -> dict:
    """Hops below the shipment records for every party."""
    upstream = {sid: e["upstream"] for sid, e in scn["sim_truth"].items()}
    upstream["corner-mart"] = "queen-st-grocer"

    def depth(sid):
        return 0 if upstream.get(sid) is None else 1 + depth(upstream[sid])
    return {sid: depth(sid) for sid in upstream}


def _check_graph(scn: dict) -> list[str]:
    errors = []
    truth, directory = scn["sim_truth"], scn["directory"]
    inbound = Counter()
    for sid, entry in truth.items():
        for f in entry["truth"].get("forwards") or []:
            inbound[f["to_party_id"]] += 1
            if resolve_party(f["to_name"], directory)[0] != f["to_party_id"]:
                errors.append(f"{sid} names {f['to_name']!r}, which does not resolve to {f['to_party_id']}")
            dest = truth.get(f["to_party_id"])
            if dest is not None and dest["truth"].get("received") != f["qty"]:
                errors.append(f"{f['to_party_id']} received {dest['truth'].get('received')}, {sid} sent {f['qty']}")
            if dest is not None and dest["upstream"] != sid:
                errors.append(f"{f['to_party_id']} names {dest['upstream']} as its sender, not {sid}")
    for d in directory:
        if d.get("tier") == 3 and inbound[d["party_id"]] != 1:
            errors.append(f"{d['party_id']} is named by {inbound[d['party_id']]} forwarders, expected 1")
    depths = _depths(scn)
    deep = [sid for sid, n in depths.items() if n >= 3]
    if len(deep) != 1 or max(depths.values()) != 3:
        errors.append(f"expected exactly one party three hops below the shipment records, got {deep}")
    forwarders = {sid for sid, e in truth.items() if e["truth"].get("forwards")}
    if forwarders != {sid for sid, e in truth.items() if e["profile"] == "forwarder"}:
        errors.append("every forwarder, and only forwarders, must forward")
    return errors


def _check_params(scn: dict) -> list[str]:
    errors = []
    truth = scn["sim_truth"]
    for sid, entry in truth.items():
        p, t, profile = entry["params"], entry["truth"], entry["profile"]
        if profile == "short_receiver":
            sender = truth.get(p.get("sender"), {})
            lies = [m for m in sender.get("params", {}).get("misreport", []) if m["to_party_id"] == sid]
            if sender.get("profile") != "forwarder" or p.get("sender") != entry["upstream"]:
                errors.append(f"{sid} must sit downstream of its simulated forwarder")
            elif len(lies) != 1 or lies[0]["extra"] != p.get("short_by") or not p.get("short_by"):
                errors.append(f"{sid}'s short_by must match its sender's first-call over-report")
            elif sender["truth"]["held"] < p["short_by"]:
                errors.append(f"{p['sender']} has too few tins on its shelf to over-report {p['short_by']} on the "
                              "first call")
        if profile == "unreachable" and (entry["tier"] != 2 or
                                         p.get("rep_reports_at") != scn["schedule"]["rep_report_at"]):
            errors.append(f"{sid}: the unreachable party must be a distributor account reported for on day 8")
        if profile == "reviser" and not (1 <= (p.get("revise") or {}).get("delta", 0) <= t["held"]):
            errors.append(f"{sid}: a revision cannot move more than the tins held")
        if profile == "over_reporter" and p.get("over_report_by", 0) <= 0:
            errors.append(f"{sid}: over_report_by must be positive")
        if profile == "callback" and not 7 <= p.get("callback_hour", -1) <= 18:
            errors.append(f"{sid}: callback_hour must be inside the working day")
        if p.get("form_after_hours") is not None and len(t.get("forwards") or []) > 1:
            errors.append(f"{sid}: the form holds one destination, so a multi-destination forwarder cannot use it")
        if profile == "no_answer" and not (p.get("answer_on_attempt") or p.get("answer_hours")):
            errors.append(f"{sid}: a no_answer party must answer on some attempt or in some hours")
    return errors


def _check_removal_scripts(scn: dict) -> list[str]:
    """Every simulated store outside the slice pulls its tins 4 to 12 simulated
    hours after it hears of the recall, and has tins to pull."""
    errors = []
    lo, hi = REMOVAL_HOURS
    for sid, entry in scn["sim_truth"].items():
        if entry["tier"] == 1 or entry["profile"] == "own_copy":
            continue
        removal = entry["truth"].get("removal") or {}
        if not lo <= removal.get("after_h", -1) <= hi:
            errors.append(f"{sid}: a removal script pulls the tins {lo:g} to {hi:g} simulated hours in")
        if removal.get("batch") != scn["recall"]["batch_code"]:
            errors.append(f"{sid}: the batch code read from its tins must be the recalled batch")
        if entry["truth"].get("held", 0) <= 0:
            errors.append(f"{sid}: a store following a removal script has tins on the shelf to pull")
    return errors


def _check_summit(scn: dict) -> list[str]:
    errors = []
    summit = [(sid, e) for sid, e in scn["sim_truth"].items() if e["profile"] == "own_copy"]
    if len(summit) != 1:
        return [f"expected one own_copy party, got {len(summit)}"]
    sid, entry = summit[0]
    if (entry["name"], entry["tier"], entry["upstream"], entry.get("channel")) != \
            (SUMMIT["name"], 2, "dist-east", "agent"):
        errors.append(f"{sid}: Summit Foods is an East account with channel agent")
    if entry["truth"]["received"] != SUMMIT["received"]:
        errors.append(f"{sid}: Summit Foods received {entry['truth']['received']}, not {SUMMIT['received']}")
    reports = entry["params"].get("reports") or []
    if [r["seq"] for r in reports] != list(range(1, len(reports) + 1)) or \
            any(b["at"] <= a["at"] for a, b in zip(reports, reports[1:])):
        errors.append(f"{sid}: reports must have seq 1..n in time order")
    for r in reports:
        b, ev = r["buckets"], r["evidence"]
        if sum(b[k] for k in REPORT_BUCKETS) != r["units_received"] or min(b.values()) < 0:
            errors.append(f"{sid} report {r['seq']}: buckets do not add up to {r['units_received']}")
        if b["held"] > ev["held_by_system"] or b["sold"] > ev["sold_by_till"] or \
                b["on_shelf"] < ev["system_on_shelf"]:
            errors.append(f"{sid} report {r['seq']}: counts the evidence does not allow")
        if r["state"] == "closed" and (b["on_shelf"] or b["unresolved"]):
            errors.append(f"{sid} report {r['seq']}: closed with tins open")
        if not 2 * 24 <= r["at"] < 10 * 24:
            errors.append(f"{sid} report {r['seq']}: reports fall on days 2 to 9")
    if reports:
        last = reports[-1]
        final = {k: entry["truth"][k] for k in BUCKETS}
        if last["state"] != "closed" or {k: last["buckets"][k] for k in BUCKETS} != final:
            errors.append(f"{sid}: the last report must be closed with its final counts")
        if not 9 * 24 <= next(r["at"] for r in reports if r["state"] == "closed") < 10 * 24:
            errors.append(f"{sid}: Summit closes on day 9")
    else:
        errors.append(f"{sid}: Summit Foods has no reports")
    return errors


def _check_connected(scn: dict) -> list[str]:
    errors = []
    stores = scn["connected_stores"]
    if len(stores) != CONNECTED_STORES or len({s["store_system_id"] for s in stores}) != len(stores):
        errors.append(f"{len(stores)} connected stores, expected {CONNECTED_STORES} with distinct ids")
    complaint = sorted(s["name"] for s in stores if s["complaint"])
    if complaint != sorted(COMPLAINT_STORES):
        errors.append(f"the day-0 complaints come from {COMPLAINT_STORES}, got {complaint}")
    if not any(s["party_id"] == "corner-mart" and s["page"] for s in stores):
        errors.append("Corner Mart's connected store (the store page) is missing")
    known = set(scn["sim_truth"]) | {"corner-mart"}
    if any(s["party_id"] not in known for s in stores):
        errors.append("a connected store is not a party of the run")
    return errors


def _check_contacts(scn: dict) -> list[str]:
    errors = []
    new = [a["phone"] for a in _accounts(scn)] + [d["phone"] for d in scn["directory"]
                                                  if not d.get("slice") and d.get("tier") == 3]
    slice_phones = [e.get("sim_phone") or e.get("phone") for e in [*scn["parties"], *scn["directory"]]
                    if e.get("slice")]
    phones = new + slice_phones
    bad = [p for p in phones if not re.match(FICTIONAL_PHONE, p or "")]
    if bad:
        errors.append(f"phones outside the fictional 555-01xx block: {bad}")
    if len(set(phones)) != len(phones):
        errors.append("phone numbers repeat")
    reused = set(new) & used_phones()
    if reused:
        errors.append(f"phones already used by another scenario: {sorted(reused)}")
    ids_by_name = defaultdict(set)
    for sid, name in [*((sid, e["name"]) for sid, e in scn["sim_truth"].items()),
                      *((d["party_id"], d["name"]) for d in scn["directory"]),
                      *((s["party_id"], s["name"]) for s in scn["parties"])]:
        ids_by_name[_norm(name)].add(sid)
    errors += [f"two parties are called {name!r}" for name, ids in ids_by_name.items() if len(ids) > 1]
    spoken = defaultdict(set)
    for d in scn["directory"]:
        for said in [d["name"], *d.get("aliases", [])]:
            spoken[_norm(said)].add(d["party_id"])
    errors += [f"two parties answer to {said!r}" for said, ids in spoken.items() if len(ids) > 1]
    return errors


def _check_slice(scn: dict) -> list[str]:
    base = _load(SLICE_SCENARIO)
    errors = []
    if scn["sim"] != long_run_sim(base):
        errors.append("the slice scripts must be recall_1's (without Corner Mart's removal check)")
    for s in base["parties"]:
        copied = next((x for x in scn["parties"] if x["party_id"] == s["party_id"]), None)
        if copied != s or not copied.get("slice"):
            errors.append(f"slice party {s['party_id']} must be recall_1's entry")
    corner = next((d for d in scn["directory"] if d["party_id"] == "corner-mart"), None)
    if corner != base["directory"][0]:
        errors.append("Corner Mart must be recall_1's directory entry")
    if {k: v for k, v in scn["recall"].items() if k not in ("_id", "units_total")} != \
            {k: v for k, v in base["recall"].items() if k not in ("_id", "units_total")}:
        errors.append("the recall must be recall_1's product, batch and checks")
    return errors


def _check_expected(scn: dict) -> list[str]:
    final = scn["expected"]["final"]
    parties = len(scn["parties"]) + len(_accounts(scn)) + len(_tier3_ids(scn)) + 1
    sim = scn["sim"]
    corner = sim["corner-mart"]["initial"][0]["claim"]
    dispute = sim["queen-st-grocer"]["initial"][0]["claim"]["forwarded_qty"] - corner["received_qty"]
    errors = []
    if (final["parties"], final["at_start"]) != (parties, len(scn["parties"])) or parties != 40:
        errors.append(f"expected.final parties {final['parties']}/{final['at_start']}, counted "
                      f"{parties}/{len(scn['parties'])}")
    if (final["disputed_units"], final["on_shelf"]) != (dispute, corner["on_shelf"]):
        errors.append(f"expected.final {final['disputed_units']} disputed / {final['on_shelf']} on the shelf, "
                      f"the slice leaves {dispute} / {corner['on_shelf']}")
    if final["open_units"] != final["disputed_units"] + final["on_shelf"]:
        errors.append("expected.final.open_units must be the disputed plus the tins on the shelf")
    return errors


# ------------------------------------------------------------------ cli

def render(scn: dict) -> str:
    return json.dumps(scn, indent=2, ensure_ascii=False) + "\n"


def write_summit_scenario(path: Path, seed: int) -> bool:
    """scenarios/summit.json through WS5's scenarios/summit.py, with the same
    seed. False while that module does not exist yet."""
    try:
        from scenarios.summit import write_summit  # noqa: PLC0415 - optional until WS5 lands it
    except ImportError:
        return False
    write_summit(path, seed)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write scenarios/recall_long.json (checked before writing).")
    parser.add_argument("--seed", type=int, default=SEED, help=f"PRNG seed (the committed file uses {SEED})")
    parser.add_argument("--out", type=Path, default=OUT, help="where to write the scenario")
    parser.add_argument("--summit-out", type=Path, default=None,
                        help="where to write summit.json (default: next to --out)")
    args = parser.parse_args(argv)
    scn = build(args.seed)
    check(scn)
    args.out.write_text(render(scn), encoding="utf-8", newline="\n")
    summit = write_summit_scenario(args.summit_out or args.out.parent / SUMMIT_OUT.name, args.seed)
    profiles = Counter(e["profile"] for e in scn["sim_truth"].values())
    print(f"wrote {args.out} (seed {args.seed}): {scn['expected']['final']['parties']} parties, "
          f"{len(scn['sim_truth'])} simulated, profiles {dict(profiles)}"
          + ("" if summit else "; summit.json not written (scenarios/summit.py not there yet)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
