"""Generate scenarios/recall_long.json: the long run's 40 parties.

    python scenarios/generate.py --seed 20261114 [--out PATH]

Batch B-4471 of DEMO-7 fire alarm control panels, 2,400 panels. Five parties
are on the shipment records: the three installers of the live slice (Harbour
Fire 40, Queen St Alarm 120, Lakeside Systems 80, with their recall_1
scripts) and two distributors (North 1,300, East 860) that sold everything
to 21 installers listed in their order systems. Six forwarders (installers
that installed panels at buildings, or passed them to another installer) name
14 parties on no list: Corner Properties from the slice, 12 more building
owners and one further installer. One chain runs three hops below the
shipment records (Distributor North -> Birchmount Fire Protection -> Juniper
Lane Alarm -> Willow Creek Towers). The 36 simulated parties each have a
behaviour profile, a ground truth that conserves, and the profile's
parameters (levela/providers/profiles.py plays them). Every building owner
follows a fix script: its panels are installed on software 2.3 or 2.4 at
first, and the installer updates them to 2.5 two to four simulated days into
the run. The 24 connected panels sit at building owners' sites; three of them
fail their monthly test on day 0 (the signal), and Corner Properties' one is
the simulated panel page.

Names and the shape of the graph are written out below; the seed only picks
the numbers (quantities, hours, versions) and the fictional phone numbers.
The generator uses its own small PRNG (splitmix64) rather than `random`, so
every Python version writes the same file. check() refuses to write a
scenario that breaks any invariant the run depends on: conservation for every
party and for the whole batch, the profile counts, the order system totals,
the one three-hop chain, short receivers downstream of a forwarder that
corrects itself on follow-up, the connected panels, fictional phones only.
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
SERIAL_PREFIX = "D7-4471-"
CORNER_PANEL = {"serial": "D7-4471-0193", "site_label": "Corner Properties, Tower 2"}
# The three connected panels that fail their monthly test on day 0, one per site.
SIGNAL_SITES = ("Maple Court", "Bayview Annex", "Spadina Works")
CONNECTED_PANELS = 24
# A building owner's installer updates its panels this many simulated days into the run.
FIX_DAYS = (2.0, 4.0)

PROFILE_COUNTS = {"straight": 14, "no_answer": 5, "callback": 3, "over_reporter": 3, "reviser": 2,
                  "forwarder": 6, "short_receiver": 2, "unreachable": 1}
SLICE_PROFILES = {"queen-st-alarm": "forwarder", "lakeside-systems": "straight"}
DISPUTED_SLICE_UNITS = 15       # Queen St says 40 went to Corner Properties, Corner Properties says 25
SLICE_AWAITING_FIX = 25         # Corner Properties: all 25 installed, the display shows 2.4

DISTRIBUTORS = [
    {"party_id": "dist-north", "name": "Distributor North", "order_system": "north", "prefix": "N",
     "shipped_in": 1300, "delivery_note": "DN-48216",
     "profile": "Regional fire and security distributor serving installers north and west of the city. Ships "
                "panels by the pallet every week. Holds and account lists go through its Kestrel OrderDesk account."},
    {"party_id": "dist-east", "name": "Distributor East", "order_system": "east", "prefix": "E",
     "shipped_in": 860, "delivery_note": "DN-48217",
     "profile": "Regional fire and security distributor serving installers east of the city, out to Durham. "
                "Ships panels by the pallet twice a month. Holds and account lists go through its Kestrel "
                "OrderDesk account."},
]

# Distributor accounts, all installers (tier 2): (order system, name, profile, variant, where).
ACCOUNTS = [
    ("north", "Birchmount Fire Protection", "forwarder", None,
     "Fire alarm contractor on Birchmount Rd in Scarborough with a warehouse and two install crews"),
    ("north", "Pinecrest Alarm Services", "forwarder", None,
     "Alarm installer on Pinecrest Ave in North York that works for property managers"),
    ("north", "Tallpine Fire & Safety", "unreachable", None,
     "One-van fire safety contractor on a rural route north of the city"),
    ("north", "Morrow Avenue Alarms", "no_answer", "hours",
     "Small alarm installer on Morrow Ave in the Junction Triangle"),
    ("north", "Elmvale Fire Systems", "no_answer", "attempt",
     "Fire systems contractor on Elmvale Rd in East York"),
    ("north", "Cedarbrae Life Safety", "callback", None,
     "Life safety contractor in the Cedarbrae business park in Scarborough"),
    ("north", "Ridgeway Alarm Co", "over_reporter", None,
     "Alarm and intercom installer on Ridgeway Dr in Etobicoke"),
    ("north", "Glenholme Fire Services", "reviser", None,
     "Family-run fire services firm on Glenholme Ave in Oakwood Village"),
    ("north", "Lambton Mills Alarm", "straight", None,
     "Alarm installer on Old Dundas St by the Humber River"),
    ("north", "Bloor Alarm", "straight", None,
     "Fire alarm contractor on Bloor St West with a busy service desk"),
    ("north", "Humber Bay Fire", "straight", None,
     "Fire alarm installer working the condo towers of Humber Bay"),
    ("north", "Silverthorn Protection", "straight", None,
     "Fire protection contractor on Silverthorn Ave near Eglinton Ave West"),
    ("east", "Fernbrook Fire Supply", "forwarder", None,
     "Fire alarm contractor in an industrial unit in Pickering that installs for housing providers"),
    ("east", "Annex Fire", "forwarder", None,
     "Fire alarm installer in the Annex that services lofts and offices downtown"),
    ("east", "Brookbanks Alarm", "no_answer", "hours",
     "Small alarm installer on Brookbanks Dr in Don Mills"),
    ("east", "Thistledown Fire", "callback", None,
     "Fire alarm contractor in a strip plaza in Thistletown, Etobicoke"),
    ("east", "Cliffcrest Alarm Systems", "over_reporter", None,
     "Alarm systems installer on Kingston Rd in Cliffcrest"),
    ("east", "Wexford Fire & Security", "straight", None,
     "Fire and security contractor on Lawrence Ave East in Wexford"),
    ("east", "Guildwood Alarm", "straight", None,
     "Owner-run alarm installer in Guildwood Village"),
    ("east", "Orchard Park Fire", "straight", None,
     "Fire alarm contractor on Orchard Park Blvd in Ajax"),
    ("east", "Highland Creek Protection", "straight", None,
     "Fire protection contractor on Old Kingston Rd in Highland Creek"),
]

# Parties on no list (tier 3): (name, type, profile, variant, sender name, where, aliases).
PARTIES = [
    ("Juniper Lane Alarm", "installer", "forwarder", None, "Birchmount Fire Protection",
     "Small alarm installer on Juniper Lane in Scarborough that subcontracts for larger firms",
     ["juniper lane alarm", "the juniper lane alarm", "juniper lane", "juniper alarm"]),
    ("Willow Creek Towers", "owner", "straight", None, "Juniper Lane Alarm",
     "Two residential towers by Willow Creek park in Scarborough",
     ["willow creek towers", "the willow creek towers", "willow creek", "willow creek tower"]),
    ("Bayview Annex", "owner", "no_answer", "hours", "Birchmount Fire Protection",
     "Office and retail annex at the top of Bayview Ave",
     ["bayview annex", "the bayview annex", "bayview", "bay view annex"]),
    ("Larkspur Residences", "owner", "short_receiver", None, "Birchmount Fire Protection",
     "Seniors' residence on Larkspur Ave with three wings",
     ["larkspur residences", "the larkspur residences", "larkspur", "larkspur residence"]),
    ("Kingsway Apartments", "owner", "callback", None, "Pinecrest Alarm Services",
     "Apartment buildings on Bloor St West in the Kingsway",
     ["kingsway apartments", "the kingsway apartments", "kingsway", "kingsway apts"]),
    ("Old Mill Condominiums", "owner", "short_receiver", None, "Pinecrest Alarm Services",
     "Condominium buildings near the Old Mill bridge",
     ["old mill condominiums", "the old mill condominiums", "old mill condos", "oldmill condominiums"]),
    ("Rosehill Court", "owner", "over_reporter", None, "Pinecrest Alarm Services",
     "Housing co-operative on Rosehill Ave by the reservoir park",
     ["rosehill court", "the rosehill court", "rosehill", "rose hill court"]),
    ("Harrow Street Lofts", "owner", "reviser", None, "Fernbrook Fire Supply",
     "Converted warehouse lofts on Harrow St in Pickering village",
     ["harrow street lofts", "the harrow street lofts", "harrow st lofts", "harrow lofts"]),
    ("Clover Hill Plaza", "owner", "no_answer", "attempt", "Fernbrook Fire Supply",
     "Shopping plaza and offices at the Clover Hill crossroads",
     ["clover hill plaza", "the clover hill plaza", "clover hill", "cloverhill plaza"]),
    ("Maple Court", "owner", "straight", None, "Fernbrook Fire Supply",
     "Mid-rise residential court on Maple Ave in Pickering",
     ["maple court", "the maple court", "maple court apartments", "maple ct"]),
    ("Dundas Lofts", "owner", "straight", None, "Annex Fire",
     "Loft building on Dundas St West downtown",
     ["dundas lofts", "the dundas lofts", "dundas loft", "dundas st lofts"]),
    ("Spadina Works", "owner", "straight", None, "Annex Fire",
     "Office building in a converted factory on Spadina Ave",
     ["spadina works", "the spadina works", "spadina", "spadina work"]),
    ("Gable Street Housing Co-op", "owner", "straight", None, "Annex Fire",
     "Housing co-operative of four low-rise buildings on Gable St in Ajax",
     ["gable street housing co-op", "the gable street housing co-op", "gable street co-op",
      "gable st housing co-op"]),
]

# What a forwarder calls a party when it is not the party's own name.
SAID_AS = {"Gable Street Housing Co-op": "Gable Street Co-op", "Maple Court": "Maple Court Apartments"}
# The forwarder that passes its panels on gets the biggest share of its sender's forwards.
ONWARD_WEIGHT = 1.6
# Building names for a building owner's connected panels (its site labels).
BUILDINGS = ("Tower 1", "Tower 2", "North Wing", "South Wing", "Block A", "Block B")

HABITS = {
    ("straight", None): "The office answers the phone during working hours and keeps panel counts in its job "
                        "system.",
    ("no_answer", "hours"): "The technicians are out on site in the morning; the office phone is only answered "
                            "between {start}:00 and {end}:00.",
    ("no_answer", "attempt"): "One person runs the office alone; the phone often rings out or is engaged and is "
                              "usually picked up only after a couple of tries.",
    ("callback", None): "The manager keeps the job sheets in the site office and asks to be called back the next "
                        "morning at {hour}:00.",
    ("over_reporter", None): "Counts are read off a shared stock sheet, and the first figure given is often from "
                             "the wrong line.",
    ("reviser", None): "Keeps careful paperwork and sends a correction when panels are returned after a call.",
    ("forwarder", None): "Installs panels at buildings for property managers, and passes spare panels to smaller "
                         "installers, none of them on a distributor's list.",
    ("short_receiver", None): "Has its panels installed by a local contractor and checks every invoice against the "
                              "panels on site.",
    ("unreachable", None): "The line is unreliable and messages go unanswered; the distributor's sales rep visits "
                           "weekly and knows the stock.",
}

BUCKETS = ("in_stock", "installed", "fixed", "returned")


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


def bucket_split(rng: Rng, kept: int, party_type: str) -> dict:
    """What a party did with the panels it kept. An installer holds them in
    stock or has sent them back; a building owner has them installed, with a
    few spares in stock and sometimes a few returned."""
    if party_type == "owner":
        weights = [rng.uniform(0.04, 0.12), rng.uniform(0.8, 0.95), 0.0,
                   rng.uniform(0.03, 0.08) if rng.random() < 0.3 else 0.0]
    else:
        weights = [rng.uniform(0.4, 0.8), 0.0, 0.0, rng.uniform(0.2, 0.6)]
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
    recall_1 scripts (the last answer each party gives; Corner Properties is
    never updated in the long run)."""
    return {"harbour-fire": last_claim(sim["harbour-fire"]["initial"]),
            "queen-st-alarm": last_claim(sim["queen-st-alarm"]["dispute_followup"]),
            "lakeside-systems": last_claim(sim["lakeside-systems"]["initial"]),
            "corner-properties": last_claim(sim["corner-properties"]["initial"])}


def long_run_sim(base: dict) -> dict:
    """recall_1's scripts without Corner Properties' fix check: in the long
    run its panels stay installed on 2.4, so a fix check replays its first
    answer and the slice ends with 25 panels awaiting a fix."""
    sim = json.loads(json.dumps(base["sim"]))
    sim["corner-properties"].pop("fix_check", None)
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
    _fix_scripts(rng, nodes, recall)
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
            "qty_in_stock": node["truth"]["in_stock"]})

    directory = [corner] + [{
        "party_id": sid, "name": node["name"], "aliases": node["aliases"], "type": node["type"], "tier": 3,
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
        sim_truth[sid] = {"name": node["name"], "type": node["type"], "tier": node["tier"],
                          "upstream": node["upstream"], "profile": node["profile"],
                          "description": _profile_text(node, params[sid]),
                          "truth": {**node["truth"], "other_batches": batches[sid]}, "params": params[sid]}

    shipment_parties = slice_parties + [{
        "party_id": d["party_id"], "name": d["name"], "type": "distributor", "tier": 1, "channel": "order_system",
        "order_system": d["order_system"], "shipped_in": d["shipped_in"], "delivery_note": d["delivery_note"],
        "profile": d["profile"],
    } for d in DISTRIBUTORS]
    total = len(shipment_parties) + len(nodes) + 1

    return {
        "scenario_id": "recall_long",
        "synthetic": True,
        "about": "The long run. Batch B-4471 of DEMO-7 fire alarm panels, 2,400 panels, 40 parties over 10 "
                 "simulated days: 5 on the shipment records, 21 installers found in the distributors' order "
                 "systems, 14 named by forwarders (12 building owners, one more installer and Corner "
                 "Properties). 36 are simulated parties with behaviour profiles; Harbour Fire and Corner "
                 "Properties answer real phones in the demo. Every building owner's panels are installed at "
                 "first and fixed two to four simulated days in. Ends with only the slice's panels open (15 "
                 "disputed, 25 installed and not fixed at Corner Properties), so the recall cannot close.",
        "seed": seed,
        "recall": recall,
        "parties": shipment_parties,
        "directory": directory,
        "order_systems": order_systems,
        "connected_panels": _connected_panels(rng, nodes, recall),
        "sim": sim,
        "sim_truth": sim_truth,
        "schedule": json.loads(json.dumps(SCHEDULE)),
        "expected": {
            "note": "Every simulated party's truth conserves and the batch adds up to units_total (checked by "
                    "generate.py before writing). final: what the run ends with when the slice's follow-ups are "
                    "manual (demo): the Queen St Alarm -> Corner Properties dispute stays open and Corner "
                    "Properties' 25 panels stay installed on 2.4.",
            "final": {"open_units": DISPUTED_SLICE_UNITS + SLICE_AWAITING_FIX, "disputed_units": DISPUTED_SLICE_UNITS,
                      "awaiting_fix": SLICE_AWAITING_FIX, "parties": total, "at_start": len(shipment_parties)},
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
    """Each distributor's panels over its accounts; forwarders get the big shares."""
    out = {}
    for dist in DISTRIBUTORS:
        rows = [a for a in ACCOUNTS if a[0] == dist["order_system"]]
        weights = [1.8 if a[2] == "forwarder" else rng.uniform(0.7, 1.3) for a in rows]
        for (order_system, name, profile, variant, where), qty in zip(rows, split(dist["shipped_in"], weights)):
            out[slugify(name)] = {"name": name, "type": "installer", "order_system": order_system,
                                  "profile": profile, "variant": variant, "where": where, "tier": 2,
                                  "upstream": dist["party_id"], "truth": {"received": qty}}
    return out


def _flows(rng: Rng, accounts: dict) -> dict:
    """Every account's and party's forwards and buckets, in table order (a party
    that forwards appears before the party it supplies). Returns the parties."""
    parties = {slugify(s[0]): {"name": s[0], "type": s[1], "profile": s[2], "variant": s[3], "sender": s[4],
                               "where": s[5], "aliases": s[6], "tier": 3, "upstream": slugify(s[4]), "truth": {}}
               for s in PARTIES}
    nodes = {**accounts, **parties}
    for sid, node in nodes.items():
        if "received" not in node["truth"]:
            node["truth"]["received"] = _received(nodes, sid)
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
        node["truth"].update(bucket_split(rng, kept, node["type"]))
        node["truth"]["forwards"] = forwards
    return parties


def _received(nodes: dict, sid: str) -> int:
    sender = nodes[nodes[sid]["upstream"]]
    return next(f["qty"] for f in sender["truth"]["forwards"] if f["to_party_id"] == sid)


def _fix_scripts(rng: Rng, nodes: dict, recall: dict) -> None:
    """Every building owner's fix script: installed on an affected version at
    first, updated to the fixed version FIX_DAYS into the run."""
    start = SCHEDULE["sim_start"]
    versions = recall["affected_versions"]
    for node in nodes.values():
        if node["type"] != "owner":
            continue
        node["truth"]["fix"] = {
            "at": round(start + 24.0 * rng.uniform(*FIX_DAYS), 1), "model": recall["model"],
            "version_before": versions[rng.randint(0, len(versions) - 1)], "version_after": recall["fixed_version"]}


def _params(rng: Rng, nodes: dict) -> dict:
    """Each profile's parameters. A short receiver's sender over-reports that
    one forward on its first call (taking the panels from `in_stock`) and
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
            p = {"revise": {"after_hours": 48.0, "delta": max(1, min(10, truth["in_stock"] // 2))}}
        elif profile == "unreachable":
            p = {"rep_reports_at": SCHEDULE["rep_report_at"], "form_after_hours": None}
        params[sid] = p
    for sid, node in nodes.items():
        if node["profile"] != "short_receiver":
            continue
        sender_id = node["upstream"]
        extra = min(4 + rng.randint(0, 4), nodes[sender_id]["truth"]["in_stock"])
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


def _connected_panels(rng: Rng, nodes: dict, recall: dict) -> list[dict]:
    """The 24 connected panels: Corner Properties' one (the simulated panel
    page), one at each signal site, and the rest spread over the other
    building owners' sites, never more than an owner has installed. Each
    panel reports on its owner's version until the owner's fix, when it is
    updated (`fixed_at`); the signal sites' panels fail on day 0."""
    owners = {sid: n for sid, n in nodes.items() if n["type"] == "owner"}
    by_name = {n["name"]: sid for sid, n in owners.items()}
    count = Counter({by_name[name]: 1 for name in SIGNAL_SITES})
    others = sorted(set(owners) - set(count))
    while sum(count.values()) < CONNECTED_PANELS - 1:
        sid = others[rng.randint(0, len(others) - 1)]
        if count[sid] < min(3, owners[sid]["truth"]["installed"]):
            count[sid] += 1
    panels = [{"serial": CORNER_PANEL["serial"], "site_label": CORNER_PANEL["site_label"],
               "party_id": "corner-properties", "model": recall["model"], "sw_version": "2.4", "fault": True,
               "signal": False, "fixed_at": None}]
    numbers = iter(sorted(rng.randint(1, 2399) for _ in range(CONNECTED_PANELS * 2)))
    used = {CORNER_PANEL["serial"]}
    for sid in sorted(count):
        node = owners[sid]
        for i in range(count[sid]):
            serial = _serial(numbers, used)
            signal = node["name"] in SIGNAL_SITES and i == 0
            site = node["name"] if signal else f"{node['name']}, {BUILDINGS[i % len(BUILDINGS)]}"
            panels.append({"serial": serial, "site_label": site, "party_id": sid, "model": recall["model"],
                           "sw_version": node["truth"]["fix"]["version_before"], "fault": signal, "signal": signal,
                           "fixed_at": node["truth"]["fix"]["at"]})
    return sorted(panels, key=lambda p: p["serial"])


def _serial(numbers, used: set) -> str:
    for n in numbers:
        serial = f"{SERIAL_PREFIX}{n:04d}"
        if serial not in used:
            used.add(serial)
            return serial
    raise ScenarioInvalid("ran out of serial numbers")


# ------------------------------------------------------------------ checks

def check(scn: dict) -> None:
    """Raise ScenarioInvalid listing every broken invariant."""
    errors: list[str] = []
    for rule in (_check_profiles, _check_order_systems, _check_conservation, _check_graph, _check_params,
                 _check_fix_scripts, _check_panels, _check_contacts, _check_slice, _check_expected):
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
            if entry.get("type") != "installer":
                errors.append(f"{a['name']} must be an installer")
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
    held = sum(sum(int(e["truth"].get(b, 0)) for b in BUCKETS) for e in scn["sim_truth"].values())
    held += sum(int(truths[s].get(b, 0)) for s in ("harbour-fire", "corner-properties") for b in BUCKETS)
    if held != scn["recall"]["units_total"]:
        errors.append(f"the batch does not add up: {held} panels held, {scn['recall']['units_total']} shipped")
    shipped = sum(s["shipped_in"] for s in scn["parties"])
    if shipped != scn["recall"]["units_total"]:
        errors.append(f"shipment records total {shipped}, units_total {scn['recall']['units_total']}")
    return errors


def _depths(scn: dict) -> dict:
    """Hops below the shipment records for every party."""
    upstream = {sid: e["upstream"] for sid, e in scn["sim_truth"].items()}
    upstream["corner-properties"] = "queen-st-alarm"

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
    if any(truth[sid]["type"] != "installer" for sid in forwarders):
        errors.append("only installers forward panels")
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
            elif sender["truth"]["in_stock"] < p["short_by"]:
                errors.append(f"{p['sender']} holds too few in stock to over-report {p['short_by']} on the "
                              "first call")
        if profile == "unreachable" and (entry["tier"] != 2 or
                                         p.get("rep_reports_at") != scn["schedule"]["rep_report_at"]):
            errors.append(f"{sid}: the unreachable party must be a distributor account reported for on day 8")
        if profile == "reviser" and not (1 <= (p.get("revise") or {}).get("delta", 0) <= t["in_stock"]):
            errors.append(f"{sid}: a revision cannot move more than the panels in stock")
        if profile == "over_reporter" and p.get("over_report_by", 0) <= 0:
            errors.append(f"{sid}: over_report_by must be positive")
        if profile == "callback" and not 7 <= p.get("callback_hour", -1) <= 18:
            errors.append(f"{sid}: callback_hour must be inside the working day")
        if p.get("form_after_hours") is not None and len(t.get("forwards") or []) > 1:
            errors.append(f"{sid}: the form holds one destination, so a multi-destination forwarder cannot use it")
        if profile == "no_answer" and not (p.get("answer_on_attempt") or p.get("answer_hours")):
            errors.append(f"{sid}: a no_answer party must answer on some attempt or in some hours")
    return errors


def _check_fix_scripts(scn: dict) -> list[str]:
    """Building owners install and are fixed later; installers install nothing of their own."""
    errors = []
    recall, start = scn["recall"], scn["schedule"]["sim_start"]
    for sid, entry in scn["sim_truth"].items():
        t, fix = entry["truth"], entry["truth"].get("fix")
        if entry["type"] == "owner":
            if not fix or t.get("installed", 0) <= 0 or t.get("fixed", 0):
                errors.append(f"{sid}: a building owner starts with panels installed and none fixed, and a fix script")
                continue
            lo, hi = (start + 24.0 * d for d in FIX_DAYS)
            if not lo <= fix["at"] <= hi:
                errors.append(f"{sid}: its fix lands at {fix['at']}, outside days {FIX_DAYS}")
            if fix["version_before"] not in recall["affected_versions"] or fix["version_after"] != recall["fixed_version"]:
                errors.append(f"{sid}: the fix must go from an affected version to the fixed version")
            if fix["model"] != recall["model"]:
                errors.append(f"{sid}: its panels must be the recalled model")
        elif fix or t.get("installed", 0) or t.get("fixed", 0):
            errors.append(f"{sid}: an installer installs at a building owner's site (a forward), never its own")
    return errors


def _check_panels(scn: dict) -> list[str]:
    errors = []
    panels, truth = scn["connected_panels"], scn["sim_truth"]
    if len(panels) != CONNECTED_PANELS:
        errors.append(f"{len(panels)} connected panels, expected {CONNECTED_PANELS}")
    if len({p["serial"] for p in panels}) != len(panels):
        errors.append("connected panel serials repeat")
    per_owner = Counter(p["party_id"] for p in panels)
    for sid, n in per_owner.items():
        if sid == "corner-properties":
            continue
        if truth.get(sid, {}).get("type") != "owner" or n > truth[sid]["truth"]["installed"]:
            errors.append(f"{sid} has {n} connected panels, more than a building owner's installed panels")
    signal = sorted(p["site_label"] for p in panels if p["signal"])
    if signal != sorted(SIGNAL_SITES):
        errors.append(f"the day-0 signal needs one failing panel at each of {SIGNAL_SITES}, got {signal}")
    if not any(p["serial"] == CORNER_PANEL["serial"] and p["party_id"] == "corner-properties" for p in panels):
        errors.append("Corner Properties' connected panel (the panel page) is missing")
    for p in panels:
        if not p["serial"].startswith(SERIAL_PREFIX) or p["model"] != scn["recall"]["model"]:
            errors.append(f"{p['serial']}: not a {scn['recall']['model']} panel of batch {scn['recall']['batch_code']}")
        if p["sw_version"] not in scn["recall"]["affected_versions"]:
            errors.append(f"{p['serial']} must start on an affected version")
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
        errors.append("the slice scripts must be recall_1's (without Corner Properties' fix check)")
    for s in base["parties"]:
        copied = next((x for x in scn["parties"] if x["party_id"] == s["party_id"]), None)
        if copied != s or not copied.get("slice"):
            errors.append(f"slice party {s['party_id']} must be recall_1's entry")
    corner = next((d for d in scn["directory"] if d["party_id"] == "corner-properties"), None)
    if corner != base["directory"][0]:
        errors.append("Corner Properties must be recall_1's directory entry")
    if {k: v for k, v in scn["recall"].items() if k not in ("_id", "units_total")} != \
            {k: v for k, v in base["recall"].items() if k not in ("_id", "units_total")}:
        errors.append("the recall must be recall_1's batch, model and checks")
    return errors


def _check_expected(scn: dict) -> list[str]:
    final = scn["expected"]["final"]
    parties = len(scn["parties"]) + len(_accounts(scn)) + len(scn["directory"])
    sim = scn["sim"]
    corner = sim["corner-properties"]["initial"][0]["claim"]
    dispute = sim["queen-st-alarm"]["initial"][0]["claim"]["forwarded_qty"] - corner["received_qty"]
    errors = []
    if (final["parties"], final["at_start"]) != (parties, len(scn["parties"])) or parties != 40:
        errors.append(f"expected.final parties {final['parties']}/{final['at_start']}, counted "
                      f"{parties}/{len(scn['parties'])}")
    if (final["disputed_units"], final["awaiting_fix"]) != (dispute, corner["installed"]):
        errors.append(f"expected.final {final['disputed_units']} disputed / {final['awaiting_fix']} awaiting fix, "
                      f"the slice leaves {dispute} / {corner['installed']}")
    if final["open_units"] != final["disputed_units"] + final["awaiting_fix"]:
        errors.append("expected.final.open_units must be the disputed plus the awaiting fix")
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
