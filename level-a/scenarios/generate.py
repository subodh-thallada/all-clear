"""Generate scenarios/recall_long.json: the long run's 40 parties.

    python scenarios/generate.py --seed 20261114 [--out PATH]

Batch B-4471 of Maple Farm milk powder 800 g, 2,400 tins (spec "Scenario at
scale"). Five parties are on the shipment records: the three stores of the
live slice (Harbour Market 40, Queen St Grocer 120, Lakeside Foods 80, with
their recall_1 scripts) and two distributors (North 1,300, East 860) that sold
everything to 21 accounts listed in their order systems: 20 stores and
Summit Foods, a six-store chain that answers through its own copy of the
agent (120 tins from East). Six forwarders (stores that passed part of their
stock to other stores) name 14 stores on no list: Corner Mart from the slice
and 13 more. One chain runs three hops below the shipment records
(Distributor North -> Birchmount Fresh Market -> Juniper Lane Variety ->
Willow Creek Convenience). The 36 simulated parties each have a behaviour
profile, a ground truth that conserves, and the profile's parameters
(levela/providers/profiles.py plays them). Every store follows a removal
script: its tins are on the shelf when it first hears of the recall, and
held four to twelve simulated hours later, after staff pull them.

The scenario also carries the store records that start the recall and prove
removals (spec "Store records"): the three complaints of simulated day 0,
about tins bought at three stores of the run, and the 14 connected stores
(Corner Mart, whose till and stock record are the simulated store page, and
13 more), each with its connector's store label and its baseline.

Names and the shape of the graph are written out below; the seed only picks
the numbers (quantities, hours) and the fictional phone numbers. The
generator uses its own small PRNG (splitmix64) rather than `random`, so every
Python version writes the same file. check() refuses to write a scenario that
breaks any invariant the run depends on: conservation for every party and for
the whole batch, the profile counts, the order system totals, the one
three-hop chain, short receivers downstream of a forwarder that corrects
itself on follow-up, the removal scripts, the store records, fictional phones
only.
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
SLICE_SCENARIO = HERE / "recall_1.json"
OTHER_SCENARIOS = (HERE / "recall_1.json", HERE / "recall_2.json")

RECALL_ID = "RCL-2026-020"
UNITS_TOTAL = 2400
SCHEDULE = {
    "sim_start": 9.0,
    "seconds_per_sim_hour": 30,
    "hold_slice_in_demo": True,
    "order_system_change_at": {"north": 81.5, "east": 82.0},
    "rep_report_at": 201.0,
    "days": 10,
}
ORDER_SYSTEM_VENDOR = "Kestrel OrderDesk"
OTHER_BATCH = "B-4480"            # the batch the demo's question asks about
AREA_CODES = ("416", "647", "437", "905", "289", "365")
FICTIONAL_PHONE = r"^\+1(416|647|437|905|289|365)55501\d\d$"
# A store's staff pull its tins this many simulated hours after it hears of the recall.
REMOVAL_HOURS = (4.0, 12.0)

PROFILE_COUNTS = {"straight": 13, "no_answer": 5, "callback": 3, "over_reporter": 3, "reviser": 2,
                  "forwarder": 6, "short_receiver": 2, "unreachable": 1, "own_copy": 1}
SLICE_PROFILES = {"queen-st-grocer": "forwarder", "lakeside-foods": "straight"}
DISPUTED_SLICE_UNITS = 15       # Queen St says 40 went to Corner Mart, Corner Mart says 25
SLICE_ON_SHELF = 25             # Corner Mart: all 25 still on the shelf

# Summit Foods, the chain that runs its own copy of the agent (spec "Sub-recall,
# agent to agent"): one of East's accounts, 120 tins. Its truth is the spec's
# report at the end of the run: 31 sold, 22 returned, the rest pulled.
OWN_COPY = {"name": "Summit Foods", "received": 120, "sold": 31, "returned": 22}

DISTRIBUTORS = [
    {"party_id": "dist-north", "name": "Distributor North", "order_system": "north", "prefix": "N",
     "shipped_in": 1300, "delivery_note": "DN-48216",
     "profile": "Regional grocery distributor serving stores north and west of the city. Ships milk powder by "
                "the pallet every week. Holds and account lists go through its Kestrel OrderDesk account."},
    {"party_id": "dist-east", "name": "Distributor East", "order_system": "east", "prefix": "E",
     "shipped_in": 860, "delivery_note": "DN-48217",
     "profile": "Regional grocery distributor serving stores east of the city, out to Durham. Ships milk powder "
                "by the pallet twice a month. Holds and account lists go through its Kestrel OrderDesk account."},
]

# Distributor accounts (tier 2): (order system, name, profile, variant, where).
ACCOUNTS = [
    ("north", "Birchmount Fresh Market", "forwarder", None,
     "Grocer on Birchmount Rd in Scarborough with a warehouse that supplies small stores nearby"),
    ("north", "Pinecrest Grocers", "forwarder", None,
     "Family grocer on Pinecrest Ave in North York that sells stock on to corner stores"),
    ("north", "Tallpine General Store", "unreachable", None,
     "General store on a rural route north of the city"),
    ("north", "Morrow Avenue Market", "no_answer", "hours",
     "Small market on Morrow Ave in the Junction Triangle"),
    ("north", "Elmvale Foods", "no_answer", "attempt",
     "Neighbourhood food store on Elmvale Rd in East York"),
    ("north", "Cedarbrae Supermarket", "callback", None,
     "Supermarket in the Cedarbrae plaza in Scarborough"),
    ("north", "Ridgeway Food Co", "over_reporter", None,
     "Food store on Ridgeway Dr in Etobicoke"),
    ("north", "Glenholme Grocery", "reviser", None,
     "Family-run grocery on Glenholme Ave in Oakwood Village"),
    ("north", "Lambton Mills Market", "straight", None,
     "Market on Old Dundas St by the Humber River"),
    ("north", "Bloor Foods", "straight", None,
     "Grocer on Bloor St West with a busy front end"),
    ("north", "Humber Bay Grocer", "straight", None,
     "Grocer serving the condo towers of Humber Bay"),
    ("north", "Silverthorn Market", "straight", None,
     "Market on Silverthorn Ave near Eglinton Ave West"),
    ("east", "Fernbrook Food Town", "forwarder", None,
     "Grocer in Pickering that sells stock on to small stores in the village"),
    ("east", "Annex Grocer", "forwarder", None,
     "Grocer in the Annex that supplies corner stores downtown"),
    ("east", "Brookbanks Market", "no_answer", "hours",
     "Small market on Brookbanks Dr in Don Mills"),
    ("east", "Thistletown Foods", "callback", None,
     "Food store in a strip plaza in Thistletown, Etobicoke"),
    ("east", "Cliffcrest Grocery", "over_reporter", None,
     "Grocery on Kingston Rd in Cliffcrest"),
    ("east", OWN_COPY["name"], "own_copy", None,
     "Six-store grocery chain in Durham Region with its own depot, which runs its own copy of the agent"),
    ("east", "Guildwood Market", "straight", None,
     "Owner-run market in Guildwood Village"),
    ("east", "Orchard Park Foods", "straight", None,
     "Food store on Orchard Park Blvd in Ajax"),
    ("east", "Highland Creek Grocer", "straight", None,
     "Grocer on Old Kingston Rd in Highland Creek"),
]

# Stores on no list (tier 3): (name, profile, variant, sender name, where, aliases).
PARTIES = [
    ("Juniper Lane Variety", "forwarder", None, "Birchmount Fresh Market",
     "Variety store on Juniper Lane in Scarborough that sells stock on to a neighbour",
     ["juniper lane variety", "the juniper lane variety", "juniper lane", "juniper variety"]),
    ("Willow Creek Convenience", "straight", None, "Juniper Lane Variety",
     "Convenience store by Willow Creek park in Scarborough",
     ["willow creek convenience", "the willow creek convenience", "willow creek", "willow creek store"]),
    ("Bayview Grocer", "no_answer", "hours", "Birchmount Fresh Market",
     "Grocer at the top of Bayview Ave",
     ["bayview grocer", "the bayview grocer", "bayview", "bay view grocer"]),
    ("Larkspur Corner Store", "short_receiver", None, "Birchmount Fresh Market",
     "Corner store on Larkspur Ave that checks every delivery note",
     ["larkspur corner store", "the larkspur corner store", "larkspur", "larkspur store"]),
    ("Kingsway Mini Mart", "callback", None, "Pinecrest Grocers",
     "Mini mart on Bloor St West in the Kingsway",
     ["kingsway mini mart", "the kingsway mini mart", "kingsway", "kingsway mart"]),
    ("Old Mill Market", "short_receiver", None, "Pinecrest Grocers",
     "Small market near the Old Mill bridge",
     ["old mill market", "the old mill market", "old mill", "oldmill market"]),
    ("Rosehill Foods", "over_reporter", None, "Pinecrest Grocers",
     "Food store on Rosehill Ave by the reservoir park",
     ["rosehill foods", "the rosehill foods", "rosehill", "rose hill foods"]),
    ("Harrow Street Grocer", "reviser", None, "Fernbrook Food Town",
     "Grocer on Harrow St in Pickering village",
     ["harrow street grocer", "the harrow street grocer", "harrow st grocer", "harrow grocer"]),
    ("Clover Hill Variety", "no_answer", "attempt", "Fernbrook Food Town",
     "Variety store at the Clover Hill crossroads",
     ["clover hill variety", "the clover hill variety", "clover hill", "cloverhill variety"]),
    ("Maple Court Market", "straight", None, "Fernbrook Food Town",
     "Market on Maple Ave in Pickering",
     ["maple court market", "the maple court market", "maple court mkt", "maple ct market"]),
    ("Dundas Mini Mart", "straight", None, "Annex Grocer",
     "Mini mart on Dundas St West downtown",
     ["dundas mini mart", "the dundas mini mart", "dundas mart", "dundas st mini mart"]),
    ("Spadina Foods", "straight", None, "Annex Grocer",
     "Food store on Spadina Ave",
     ["spadina foods", "the spadina foods", "spadina", "spadina food"]),
    ("Gable Street Co-op Grocery", "straight", None, "Annex Grocer",
     "Co-operative grocery on Gable St in Ajax",
     ["gable street co-op grocery", "the gable street co-op grocery", "gable street co-op",
      "gable st co-op grocery"]),
]

# What a forwarder calls a store when it is not the store's own name.
SAID_AS = {"Gable Street Co-op Grocery": "Gable Street Co-op", "Maple Court Market": "Maple Court Mkt"}
# The forwarder that passes its tins on gets the biggest share of its sender's forwards.
ONWARD_WEIGHT = 1.6

# Spec "In the run", simulated day 0: three complaints about tins bought at
# three stores, each a seal fault on batch B-4471 (the signal). The stores
# are parties of the run; the shopper's words name them.
COMPLAINTS = [
    {"_id": "c-0007", "bought_at": "Maple Court Market", "reason_code": "SEAL_BROKEN", "at": 1.5,
     "text": "the foil under the lid was torn when I opened it"},
    {"_id": "c-0011", "bought_at": "Bayview Grocer", "reason_code": None, "at": 4.0,
     "text": "lid did not seal, the powder was hard and lumpy inside"},
    {"_id": "c-0012", "bought_at": "Spadina Foods", "reason_code": None, "at": 7.5,
     "text": "lid was lifted, powder clumped and smelled sour"},
]
DEFECT = "seal_fault"
# Stores whose till and stock record report to the recall (spec screen: "Stores connected 14").
CONNECTED_STORES = 14
CORNER_LABEL = "Corner Mart"

HABITS = {
    ("straight", None): "The front-end manager answers the phone during opening hours and reads counts off the "
                        "till.",
    ("no_answer", "hours"): "Staff are on the floor in the morning; the phone is only answered between {start}:00 "
                            "and {end}:00.",
    ("no_answer", "attempt"): "One person runs the store alone; the phone often rings out or is engaged and is "
                              "usually picked up only after a couple of tries.",
    ("callback", None): "The manager keeps the delivery notes in the office and asks to be called back the next "
                        "morning at {hour}:00.",
    ("over_reporter", None): "Counts are read off a shared stock sheet, and the first figure given is often from "
                             "the wrong line.",
    ("reviser", None): "Keeps careful paperwork and sends a correction when tins go back after a call.",
    ("forwarder", None): "Passes spare tins to smaller stores, none of them on a distributor's list.",
    ("short_receiver", None): "Buys from a local grocer and checks every delivery note against the tins that "
                              "arrived.",
    ("unreachable", None): "The line is unreliable and messages go unanswered; the distributor's sales rep visits "
                           "weekly and knows the stock.",
    ("own_copy", None): "Runs its own copy of the recall agent on its own records; its buyer list never leaves "
                        "the chain.",
}

BUCKETS = ("sold", "on_shelf", "held", "returned")


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
    """What a store did with the tins it kept, when it first hears of the
    recall: some sold to shoppers, most still on the shelf, and sometimes a
    few already sent back. None are held yet: that is the removal script."""
    weights = [rng.uniform(0.2, 0.45), rng.uniform(0.45, 0.75), 0.0,
               rng.uniform(0.03, 0.08) if rng.random() < 0.3 else 0.0]
    return dict(zip(BUCKETS, split(kept, weights)))


# ------------------------------------------------------------------ build

def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def used_phones() -> set[str]:
    """Numbers already in the other scenarios."""
    phones: set = set()
    for path in OTHER_SCENARIOS:
        scn = _load(path)
        for entry in [*scn["parties"], *scn.get("directory", [])]:
            phones.update({entry.get("phone"), entry.get("sim_phone")})
    return {p for p in phones if p}


def phone_book(rng: Rng, n: int) -> list[str]:
    """n distinct numbers in the fictional 555-01xx block, cycling area codes."""
    taken, out = used_phones(), []
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
    recall_1 scripts (the last answer each party gives; Corner Mart's tins
    stay on the shelf in the long run)."""
    return {"harbour-market": last_claim(sim["harbour-market"]["initial"]),
            "queen-st-grocer": last_claim(sim["queen-st-grocer"]["dispute_followup"]),
            "lakeside-foods": last_claim(sim["lakeside-foods"]["initial"]),
            "corner-mart": last_claim(sim["corner-mart"]["initial"])}


def long_run_sim(base: dict) -> dict:
    """recall_1's scripts without Corner Mart's removal check: in the long run
    its tins stay on the shelf, so a removal check replays its first answer
    and the slice ends with 25 tins still on the shelf."""
    sim = json.loads(json.dumps(base["sim"]))
    sim["corner-mart"].pop("removal_check", None)
    return sim


def build(seed: int = SEED) -> dict:
    """The whole scenario as a dict (deterministic for a seed)."""
    rng = Rng(seed)
    base = _load(SLICE_SCENARIO)
    recall = {**base["recall"], "_id": RECALL_ID, "units_total": UNITS_TOTAL}
    slice_parties = list(base["parties"])
    corner = dict(base["directory"][0])
    sim = long_run_sim(base)
    truths = slice_truths(sim)

    accounts = _accounts_shipped(rng)
    parties = _flows(rng, accounts)
    nodes = {**accounts, **parties}
    _removal_scripts(rng, nodes, recall)
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
            "qty_in_stock": node["truth"]["on_shelf"]})

    directory = [corner] + [{
        "party_id": sid, "name": node["name"], "aliases": node["aliases"], "type": "store", "tier": 3,
        "channel": "sim", "phone": node["phone"], "profile": _profile_text(node, params[sid]),
    } for sid, node in parties.items()]

    sim_truth = {}
    for sid, profile in SLICE_PROFILES.items():
        party = next(s for s in slice_parties if s["party_id"] == sid)
        sim_truth[sid] = {"name": party["name"], "type": party["type"], "tier": 1, "upstream": None,
                          "profile": profile, "description": party["profile"],
                          "truth": {**_truth_from_claim(truths[sid], base), "other_batches": batches[sid]},
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
        "about": "The long run. Batch B-4471 of Maple Farm milk powder 800 g, 2,400 tins, 40 parties over 10 "
                 "simulated days: 5 on the shipment records, 21 accounts found in the distributors' order systems "
                 "(20 stores and Summit Foods, which runs its own copy of the agent), and 14 stores named by "
                 "forwarders, Corner Mart among them. 36 are simulated parties with behaviour profiles; Harbour "
                 "Market and Corner Mart answer real phones in the demo. Every store's tins are on the shelf when "
                 "it hears of the recall and held four to twelve simulated hours later. Ends with only the slice's "
                 "tins open (15 disputed, 25 still on the shelf at Corner Mart), so the recall cannot close.",
        "seed": seed,
        "recall": recall,
        "parties": shipment_parties,
        "directory": directory,
        "order_systems": order_systems,
        "complaints": _complaints(recall),
        "store_systems": _store_systems(rng, nodes),
        "sim": sim,
        "sim_truth": sim_truth,
        "schedule": json.loads(json.dumps(SCHEDULE)),
        "expected": {
            "note": "Every simulated party's truth conserves and the batch adds up to units_total (checked by "
                    "generate.py before writing). final: what the run ends with when the slice's follow-ups are "
                    "manual (demo): the Queen St Grocer -> Corner Mart dispute stays open and Corner Mart's 25 "
                    "tins stay on the shelf.",
            "final": {"open_units": DISPUTED_SLICE_UNITS + SLICE_ON_SHELF, "disputed_units": DISPUTED_SLICE_UNITS,
                      "on_shelf": SLICE_ON_SHELF, "parties": total, "at_start": len(shipment_parties)},
            "parties_by_tier": {"1": len(shipment_parties), "2": len(accounts), "3": len(parties) + 1},
            "profiles": dict(PROFILE_COUNTS),
        },
    }


def _distributor(order_system: str) -> dict:
    return next(d for d in DISTRIBUTORS if d["order_system"] == order_system)


def _truth_from_claim(claim: dict, base: dict) -> dict:
    """A scripted claim as a truth: buckets plus its one forward, if any."""
    truth = {b: int(claim.get(b, 0)) for b in BUCKETS}
    truth["forwards"] = []
    if claim.get("forwarded_qty"):
        to_id, _ = resolve_party(claim["forwarded_to_name"], base.get("directory"))
        truth["forwards"].append({"to_party_id": to_id, "to_name": claim["forwarded_to_name"],
                                  "qty": int(claim["forwarded_qty"])})
    return truth


def _accounts_shipped(rng: Rng) -> dict:
    """Each distributor's tins over its accounts; forwarders get the big
    shares and Summit Foods its fixed 120."""
    out = {}
    for dist in DISTRIBUTORS:
        rows = [a for a in ACCOUNTS if a[0] == dist["order_system"]]
        fixed = sum(OWN_COPY["received"] for a in rows if a[2] == "own_copy")
        shared = [a for a in rows if a[2] != "own_copy"]
        weights = [1.8 if a[2] == "forwarder" else rng.uniform(0.7, 1.3) for a in shared]
        qty = dict(zip((a[1] for a in shared), split(dist["shipped_in"] - fixed, weights)))
        for order_system, name, profile, variant, where in rows:
            received = OWN_COPY["received"] if profile == "own_copy" else qty[name]
            out[slugify(name)] = {"name": name, "order_system": order_system, "profile": profile,
                                  "variant": variant, "where": where, "tier": 2, "upstream": dist["party_id"],
                                  "truth": {"received": received}}
    return out


def _flows(rng: Rng, accounts: dict) -> dict:
    """Every account's and store's forwards and buckets, in table order (a
    store that forwards appears before the store it supplies). Returns the
    tier 3 stores."""
    parties = {slugify(s[0]): {"name": s[0], "profile": s[1], "variant": s[2], "sender": s[3], "where": s[4],
                               "aliases": s[5], "tier": 3, "upstream": slugify(s[3]), "truth": {}}
               for s in PARTIES}
    nodes = {**accounts, **parties}
    for sid, node in nodes.items():
        if "received" not in node["truth"]:
            node["truth"]["received"] = _received(nodes, sid)
        if node["profile"] == "own_copy":
            sold, returned = OWN_COPY["sold"], OWN_COPY["returned"]
            node["truth"].update(sold=sold, on_shelf=node["truth"]["received"] - sold - returned, held=0,
                                 returned=returned, forwards=[])
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


def _removal_scripts(rng: Rng, nodes: dict, recall: dict) -> None:
    """Every store's removal script: its tins are on the shelf when it hears
    of the recall, and held REMOVAL_HOURS later, after staff pull them."""
    for node in nodes.values():
        node["truth"]["removal"] = {"after_hours": round(rng.uniform(*REMOVAL_HOURS), 1),
                                    "batch": recall["batch_code"]}


def _params(rng: Rng, nodes: dict) -> dict:
    """Each profile's parameters. A short receiver's sender over-reports that
    one forward on its first call (taking the tins from the shelf) and
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
            p = {"revise": {"after_hours": 48.0, "delta": max(1, min(10, truth["on_shelf"] // 2))}}
        elif profile == "unreachable":
            p = {"rep_reports_at": SCHEDULE["rep_report_at"], "form_after_hours": None}
        params[sid] = p
    for sid, node in nodes.items():
        if node["profile"] != "short_receiver":
            continue
        sender_id = node["upstream"]
        extra = min(4 + rng.randint(0, 4), nodes[sender_id]["truth"]["on_shelf"])
        params[sid] = {"short_by": extra, "sender": sender_id}
        params[sender_id].setdefault("misreport", []).append({"to_party_id": sid, "extra": extra})
    return params


def _other_batches(rng: Rng, party_ids: list[str]) -> dict:
    """About a third of the parties also took delivery of batch B-4480 (the
    demo's question: "Did you also receive batch B-4480?")."""
    return {sid: ({OTHER_BATCH: 6 + rng.randint(0, 18)} if rng.random() < 0.35 else {}) for sid in party_ids}


def _profile_text(node: dict, params: dict) -> str:
    """The party's profile text (parties.profile; what memory embeds). Accounts
    have no directory entry, so theirs is sim_truth[...].description."""
    habit = HABITS[(node["profile"], node["variant"])]
    hours = params.get("answer_hours") or [0, 0]
    return f"{node['where']}. " + habit.format(start=hours[0], end=hours[1], hour=params.get("callback_hour", 9))


def _complaints(recall: dict) -> list[dict]:
    """The day-0 complaints, as the company's complaint form posts them."""
    return [{**c, "batch_code": recall["batch_code"], "defect": DEFECT} for c in COMPLAINTS]


def _store_systems(rng: Rng, nodes: dict) -> list[dict]:
    """The connected stores: Corner Mart (the simulated store page), the three
    stores the complaints name, and the rest drawn from the other stores.
    Summit Foods is left out (its records answer through its own agent), and
    so is the unreachable store. Each connector posts a baseline when it is
    linked: tins received, sold so far and on the shelf, per the store's own
    records (the truth when it first hears of the recall)."""
    by_name = {n["name"]: sid for sid, n in nodes.items()}
    chosen = [by_name[c["bought_at"]] for c in COMPLAINTS]
    others = sorted(sid for sid, n in nodes.items()
                    if sid not in chosen and n["profile"] not in ("own_copy", "unreachable"))
    while len(chosen) < CONNECTED_STORES - 1:
        sid = others[rng.randint(0, len(others) - 1)]
        if sid not in chosen:
            chosen.append(sid)
    rows = [{"store_label": CORNER_LABEL, "party_id": "corner-mart", "connects": ["till", "stock"],
             "simulated_page": True}]
    for sid in sorted(chosen):
        t = nodes[sid]["truth"]
        rows.append({"store_label": nodes[sid]["name"], "party_id": sid, "connects": ["till", "stock"],
                     "simulated_page": False,
                     "baseline": {"received": t["received"], "sold": t["sold"], "on_shelf": t["on_shelf"]}})
    return rows


# ------------------------------------------------------------------ checks

def check(scn: dict) -> None:
    """Raise ScenarioInvalid listing every broken invariant."""
    errors: list[str] = []
    for rule in (_check_profiles, _check_order_systems, _check_conservation, _check_graph, _check_params,
                 _check_removal_scripts, _check_store_records, _check_contacts, _check_slice, _check_expected):
        try:
            errors.extend(rule(scn))
        except (KeyError, TypeError, ValueError, StopIteration, RecursionError) as exc:
            errors.append(f"{rule.__name__.removeprefix('_check_')}: the scenario is malformed ({exc!r})")
    if errors:
        raise ScenarioInvalid("recall_long is invalid:\n  " + "\n  ".join(errors))


def _accounts(scn: dict) -> list[dict]:
    return [a for order_system in scn["order_systems"].values() for a in order_system["accounts"]]


def _party_ids(scn: dict) -> list[str]:
    return [d["party_id"] for d in scn["directory"] if not d.get("slice")]


def _check_profiles(scn: dict) -> list[str]:
    truth = scn["sim_truth"]
    expected_ids = {slugify(a["name"]) for a in _accounts(scn)} | set(SLICE_PROFILES) | set(_party_ids(scn))
    errors = []
    if set(truth) != expected_ids:
        errors.append(f"sim_truth covers {sorted(set(truth) ^ expected_ids)} wrongly")
    got = Counter(e["profile"] for e in truth.values())
    if dict(got) != PROFILE_COUNTS:
        errors.append(f"profile counts {dict(got)} != {PROFILE_COUNTS}")
    for sid, profile in SLICE_PROFILES.items():
        if truth.get(sid, {}).get("profile") != profile:
            errors.append(f"{sid} must be {profile}")
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
            if entry.get("type") != "store":
                errors.append(f"{a['name']} must be a store")
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
    truths = slice_truths(scn["sim"])
    kept = sum(sum(int(e["truth"].get(b, 0)) for b in BUCKETS) for e in scn["sim_truth"].values())
    kept += sum(int(truths[s].get(b, 0)) for s in ("harbour-market", "corner-mart") for b in BUCKETS)
    if kept != scn["recall"]["units_total"]:
        errors.append(f"the batch does not add up: {kept} tins accounted for, {scn['recall']['units_total']} "
                      "shipped")
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
        if inbound[d["party_id"]] != 1:
            errors.append(f"{d['party_id']} is named by {inbound[d['party_id']]} forwarders, expected 1")
    depths = _depths(scn)
    deep = [sid for sid, n in depths.items() if n >= 3]
    if len(deep) != 1 or max(depths.values()) != 3:
        errors.append(f"expected exactly one party three hops below the shipment records, got {deep}")
    forwarders = {sid for sid, e in truth.items() if e["truth"].get("forwards")}
    if forwarders != {sid for sid, e in truth.items() if e["profile"] == "forwarder"}:
        errors.append("every forwarder, and only forwarders, must forward")
    if any(truth[sid]["type"] != "store" for sid in forwarders):
        errors.append("only stores forward tins")
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
            elif sender["truth"]["on_shelf"] < p["short_by"]:
                errors.append(f"{p['sender']} has too few on the shelf to over-report {p['short_by']} on the "
                              "first call")
        if profile == "unreachable" and (entry["tier"] != 2 or
                                         p.get("rep_reports_at") != scn["schedule"]["rep_report_at"]):
            errors.append(f"{sid}: the unreachable party must be a distributor account reported for on day 8")
        if profile == "own_copy" and (entry["tier"] != 2 or entry.get("channel") != "agent" or
                                      entry["upstream"] != "dist-east" or
                                      t["received"] != OWN_COPY["received"]):
            errors.append(f"{sid}: the own-copy party must be East's account with {OWN_COPY['received']} tins, "
                          "channel agent")
        if profile == "reviser" and not (1 <= (p.get("revise") or {}).get("delta", 0) <= t["on_shelf"]):
            errors.append(f"{sid}: a revision cannot move more than the tins on the shelf")
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
    """Every simulated store below the shipment records starts with tins on
    the shelf and none held, and pulls them REMOVAL_HOURS after it hears of
    the recall."""
    errors = []
    batch = scn["recall"]["batch_code"]
    for sid, entry in scn["sim_truth"].items():
        if entry["tier"] == 1:
            continue
        t, removal = entry["truth"], entry["truth"].get("removal")
        if not removal or t.get("on_shelf", 0) <= 0 or t.get("held", 0):
            errors.append(f"{sid}: a store starts with tins on the shelf and none held, and a removal script")
            continue
        lo, hi = REMOVAL_HOURS
        if not lo <= removal["after_hours"] <= hi:
            errors.append(f"{sid}: its tins are pulled {removal['after_hours']} h after the notice, outside "
                          f"{REMOVAL_HOURS}")
        if removal["batch"] != batch:
            errors.append(f"{sid}: its tins must be of the recalled batch {batch}")
    return errors


def _check_store_records(scn: dict) -> list[str]:
    errors = []
    truth, batch = scn["sim_truth"], scn["recall"]["batch_code"]
    names = {e["name"]: sid for sid, e in truth.items()}
    complaints = scn["complaints"]
    stores = {c["bought_at"] for c in complaints}
    if len(complaints) != 3 or len(stores) != 3:
        errors.append("the signal needs three complaints from three stores")
    for c in complaints:
        if c["bought_at"] not in names:
            errors.append(f"complaint {c['_id']} names {c['bought_at']!r}, which is not a store of the run")
        if c["batch_code"] != batch or c["defect"] != DEFECT:
            errors.append(f"complaint {c['_id']} must be a {DEFECT} on batch {batch}")
        if not 0 <= c["at"] < scn["schedule"]["sim_start"]:
            errors.append(f"complaint {c['_id']} must arrive on day 0, before the run starts")
    systems = scn["store_systems"]
    if len(systems) != CONNECTED_STORES:
        errors.append(f"{len(systems)} connected stores, expected {CONNECTED_STORES}")
    if len({s["store_label"] for s in systems}) != len(systems):
        errors.append("store labels repeat")
    if not any(s["party_id"] == "corner-mart" and s["simulated_page"] for s in systems):
        errors.append("Corner Mart's connector (the simulated store page) is missing")
    connected = {s["party_id"] for s in systems}
    if not {names.get(c["bought_at"]) for c in complaints} <= connected:
        errors.append("every store a complaint names must be connected")
    for s in systems:
        if s["party_id"] == "corner-mart":
            continue
        entry = truth.get(s["party_id"])
        if entry is None or entry["type"] != "store" or entry["profile"] in ("own_copy", "unreachable"):
            errors.append(f"{s['store_label']}: a connector belongs to a simulated store that answers for itself")
            continue
        t = entry["truth"]
        if s.get("baseline") != {"received": t["received"], "sold": t["sold"], "on_shelf": t["on_shelf"]}:
            errors.append(f"{s['store_label']}: its baseline must be the store's own records")
    return errors


def _check_contacts(scn: dict) -> list[str]:
    errors = []
    new = [a["phone"] for a in _accounts(scn)] + [d["phone"] for d in scn["directory"] if not d.get("slice")]
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
        errors.append("the recall must be recall_1's batch, product and checks")
    return errors


def _check_expected(scn: dict) -> list[str]:
    final = scn["expected"]["final"]
    parties = len(scn["parties"]) + len(_accounts(scn)) + len(scn["directory"])
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
        errors.append("expected.final.open_units must be the disputed plus the tins still on the shelf")
    return errors


# ------------------------------------------------------------------ cli

def render(scn: dict) -> str:
    return json.dumps(scn, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write scenarios/recall_long.json (checked before writing).")
    parser.add_argument("--seed", type=int, default=SEED, help=f"PRNG seed (the committed file uses {SEED})")
    parser.add_argument("--out", type=Path, default=OUT, help="where to write the scenario")
    args = parser.parse_args(argv)
    scn = build(args.seed)
    check(scn)
    args.out.write_text(render(scn), encoding="utf-8", newline="\n")
    profiles = Counter(e["profile"] for e in scn["sim_truth"].values())
    print(f"wrote {args.out} (seed {args.seed}): {scn['expected']['final']['parties']} parties, "
          f"{len(scn['sim_truth'])} simulated, profiles {dict(profiles)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
