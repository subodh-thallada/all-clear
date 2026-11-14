"""Summit Foods' scenario: what only Summit's node knows (spec "Sub-recall, agent to agent", "In the run").

    python scenarios/summit.py --seed 20261114 [--out scenarios/summit.json]

Summit Foods is a six-store chain that buys from Distributor East. It runs
its own copy of All Clear. Its depot received 120 tins of batch B-4471:
18 are still held at the depot, 22 went back to East (returned) and 80 were
delivered to its six stores. Before the recall opened, 31 of those were sold
(19 to members whose contact Summit holds, 12 anonymous). Every store is
connected (its till and stock records report to Summit's node); the store
managers are simulated with the same profiles as our parties: four Straight,
one No answer and one Over-reporter. The Over-reporter's store keeps 9 tins
on the shelf until simulated day 9 and says "all pulled" on day 5.

The numbers and names below are fixed; the seed only picks which purchases
have a member contact, which of those buyers cannot be reached, the hours of
the purchases and which store is the No answer one. Every name, phone number
and barcode is invented. WS2's scenarios/generate.py calls build_summit()
with the recall's seed and asserts the 120 are conserved.
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

SEED = 20261114
OUT = Path(__file__).resolve().parent / "summit.json"

RECEIVED = 120
DEPOT_HELD, DEPOT_RETURNED, DELIVERED = 18, 22, 80
MEMBER_PURCHASES, ANONYMOUS_PURCHASES = 19, 12
UNREACHABLE_BUYERS = 4
OVER_REPORTER_ON_SHELF = 9

PRODUCT = "Maple Farm milk powder 800 g"
BARCODE = "0 62345 18803 5"
BATCH = "B-4471"

# store id, name, received, sold before the recall
STORES = [
    ("summit-riverside", "Summit Foods Riverside", 16, 7),
    ("summit-hillview", "Summit Foods Hillview", 14, 6),
    ("summit-old-mill", "Summit Foods Old Mill", 14, 5),
    ("summit-bayfront", "Summit Foods Bayfront", 12, 5),
    ("summit-parkdale", "Summit Foods Parkdale", 12, 4),
    ("summit-elm-st", "Summit Foods Elm St", 12, 4),
]
# The Over-reporter is the store with 9 tins on the shelf when the recall opens.
OVER_REPORTER = "summit-riverside"

# Summit's item file: the recalled item and its look-alikes (for matching).
ITEMS = [
    {"item_id": "SF-10233", "description": "Maple Farm milk powder 800 g", "barcode": BARCODE},
    {"item_id": "SF-10234", "description": "Maple Farm milk powder 400 g", "barcode": "0 62345 18804 2"},
    {"item_id": "SF-10235", "description": "Maple Farm skim milk powder 800 g", "barcode": "0 62345 18807 3"},
    {"item_id": "SF-11870", "description": "Summit Select whole milk powder 800 g", "barcode": "0 71234 55102 9"},
    {"item_id": "SF-13110", "description": "Hillcrest milk powder 800 g", "barcode": "0 58810 20031 7"},
    {"item_id": "SF-12002", "description": "Maple Farm hot chocolate mix 800 g", "barcode": "0 62345 19100 4"},
]
MATCHED_ITEM = "SF-10233"


def _staff_phone(n: int) -> str:
    return f"555-01{n:02d}"


def build_summit(seed: int = SEED) -> dict:
    """Summit's scenario as a dict (deterministic for a seed). Asserts that the
    120 tins are conserved and the purchase counts are the spec's."""
    rng = random.Random(seed)
    stores = []
    others = [s[0] for s in STORES if s[0] != OVER_REPORTER]
    no_answer = rng.choice(others)
    for i, (store_id, name, received, sold) in enumerate(STORES, start=1):
        profile = "over_reporter" if store_id == OVER_REPORTER else "no_answer" if store_id == no_answer else "straight"
        on_shelf = received - sold
        stores.append({
            "store_id": store_id, "party_id": store_id, "name": name, "type": "store", "tier": 2,
            "store_system_id": f"ss-{store_id}", "received": received, "sold": sold,
            "on_shelf_at_recall": on_shelf, "profile": profile,
            "manager": {"role": "store manager", "phone": _staff_phone(10 + i)},
            # Simulated hours after the sub-recall opens when staff move each tin to
            # quarantine. The Over-reporter pulls all but 9 at once and the last 9 on day 9.
            "pull_at": 10.0 + 4 * i,
            "late_pull": ({"qty": OVER_REPORTER_ON_SHELF, "at": 9 * 24 + 9.0} if profile == "over_reporter"
                          else None),
            "all_pulled_claim_at": (5 * 24 + 10.3) if profile == "over_reporter" else None,
        })
    for s in stores:
        if s["late_pull"]:
            assert s["on_shelf_at_recall"] == OVER_REPORTER_ON_SHELF, "the Over-reporter's store keeps 9 on a shelf"

    # 31 purchases of one tin each, store by store; the seed picks the members.
    purchases = []
    for s in stores:
        for _ in range(s["sold"]):
            purchases.append({"store_id": s["store_id"], "qty": 1,
                              "bought_at": -round(rng.uniform(2.0, 140.0), 2)})
    members = set(rng.sample(range(len(purchases)), MEMBER_PURCHASES))
    member_ids = sorted(members)
    unreachable = set(rng.sample(member_ids, UNREACHABLE_BUYERS))
    buyers, anonymous = [], []
    for n, p in enumerate(purchases):
        pid = f"sp-{n + 1:03d}"
        if n in members:
            k = len(buyers) + 1
            buyers.append({"_id": f"buyer-{k:02d}", "purchase_id": pid, "store_id": p["store_id"],
                           "contact": f"555-03{k:02d}", "bought_at": p["bought_at"], "qty": p["qty"],
                           "reachable": n not in unreachable})
        else:
            anonymous.append({"purchase_id": pid, "store_id": p["store_id"], "bought_at": p["bought_at"],
                              "qty": p["qty"]})

    depot = {"party_id": "summit-depot", "name": "Summit Foods depot", "type": "self", "tier": 1,
             "received": RECEIVED, "held": DEPOT_HELD, "returned": DEPOT_RETURNED, "delivered": DELIVERED,
             "staff": {"role": "depot lead", "phone": _staff_phone(1)}}

    scn = {
        "scenario_id": "summit", "synthetic": True, "seed": seed,
        "about": "Summit Foods' own records for its sub-recall of Maple Farm milk powder 800 g, batch B-4471. "
                 "Invented. Never sent to the parent node.",
        # The business line is on East's account list, so our ledger may hold it; staff and buyer numbers never.
        "node": {"node_id": "summit", "name": "Summit Foods", "business_phone": "555-0400"},
        "parent": {"node_id": "maple-farm", "link_id": "ln-summit", "party_id": "summit-foods",
                   "match": {"system": "east", "account": "EAST-SUMMIT"}},
        "product": PRODUCT, "barcode": BARCODE, "batch_code": BATCH,
        "received": RECEIVED, "depot": depot, "stores": stores,
        "buyers": buyers, "anonymous_purchases": anonymous,
        "items": [dict(i) for i in ITEMS], "matched_item": MATCHED_ITEM,
        "expected": {
            "received": RECEIVED, "sold": sum(s["sold"] for s in stores),
            "held_final": DEPOT_HELD + sum(s["on_shelf_at_recall"] for s in stores),
            "returned": DEPOT_RETURNED, "on_shelf_day5": OVER_REPORTER_ON_SHELF,
            "held_day5": DEPOT_HELD + sum(s["on_shelf_at_recall"] for s in stores) - OVER_REPORTER_ON_SHELF,
            "buyers": {"identified": MEMBER_PURCHASES, "told": MEMBER_PURCHASES - UNREACHABLE_BUYERS,
                       "unreachable": UNREACHABLE_BUYERS, "anonymous_sales": ANONYMOUS_PURCHASES},
            "parties_total": 1 + len(stores),
        },
    }
    check(scn)
    return scn


def check(scn: dict) -> None:
    """The 120 are conserved, and the counts are the spec's."""
    d, stores = scn["depot"], scn["stores"]
    assert d["held"] + d["returned"] + d["delivered"] == scn["received"] == RECEIVED, "depot does not conserve 120"
    assert sum(s["received"] for s in stores) == d["delivered"], "stores do not add up to the 80 delivered"
    assert all(s["received"] == s["sold"] + s["on_shelf_at_recall"] for s in stores)
    e = scn["expected"]
    assert e["sold"] + e["held_final"] + e["returned"] == RECEIVED, "final buckets do not conserve 120"
    assert len(scn["buyers"]) == MEMBER_PURCHASES and len(scn["anonymous_purchases"]) == ANONYMOUS_PURCHASES
    assert len(scn["buyers"]) + len(scn["anonymous_purchases"]) == e["sold"] == 31
    profiles = sorted(s["profile"] for s in stores)
    assert profiles == sorted(["straight"] * 4 + ["no_answer", "over_reporter"]), profiles


def write_summit(path=OUT, seed: int = SEED) -> dict:
    scn = build_summit(seed)
    Path(path).write_text(json.dumps(scn, indent=2) + "\n", encoding="utf-8")
    return scn


def load_summit(path=OUT) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args(argv)
    scn = write_summit(args.out, args.seed)
    print(f"wrote {args.out}: {scn['received']} tins, {len(scn['stores'])} stores, "
          f"{len(scn['buyers'])} buyers with a contact, {len(scn['anonymous_purchases'])} anonymous")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
