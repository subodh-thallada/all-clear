"""The run's store records, posted through the API (spec "Store records" > "In the run").

    python scripts/store_script.py [--api URL] [--scenario recall_long] COMMAND

    register    create the connected stores' connectors (POST /admin/stores/{id}):
                label, batch and the stock record before the recall (received, sold,
                on the shelf). They link to their party when a claim names the store.
    complaints  day 0: seed the confirmed past complaints, then three complaints about
                tins bought at three stores (seal fault, batch B-4471) through
                POST /complaints. Prints the signal they raise (proposed).
    approve     a person approves the batch's proposed signal (--by, --recall-id,
                --all-sim): POST /admin/signals/{id}/approve. Never run by an agent.
    removals    each linked connected store (except the one with the page, which a
                teammate presses) posts its removal: one quarantine event per tin
                its staff pulled, then one scan the till refuses (blocked_sale).
                Event ids are fixed, so running it twice moves nothing twice.
    run         register, then complaints (approval is left to a person)

Every value here is invented and labelled simulated. API_BASE_URL, TOOL_SECRET
and ADMIN_TOKEN come from level-a/.env like every other process.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from levela.config import Settings  # noqa: E402
from levela.db.seed import load_scenario  # noqa: E402

BATCH = "B-4471"
BARCODE = "0 62345 18803 5"
# Day 0: three complaints about tins bought at three stores. One carries the
# form's reason code; two are free text, classified from similar past complaints.
COMPLAINT_TEXTS = [
    ("SEAL", "The seal under the lid was broken and the powder was clumped."),
    (None, "lid was lifted, powder clumped and smelled sour"),
    (None, "the seal was broken and the powder was lumpy"),
]
DEFAULT_COMPLAINT_STORES = ["Maple Court Market", "Bayview Corner Store", "Spadina Fresh"]


class Api:
    def __init__(self, base: str, settings: Settings):
        self.http = httpx.Client(base_url=base.rstrip("/"), timeout=30)
        self.tool = {"X-Tool-Secret": settings.tool_secret}
        self.admin = {"X-Admin-Token": settings.admin_token}

    def post(self, path: str, body: dict, *, admin: bool = False) -> dict:
        r = self.http.post(path, json=body, headers=self.admin if admin else self.tool)
        if r.status_code >= 400:
            raise SystemExit(f"POST {path}: {r.status_code} {r.text[:300]}")
        return r.json()

    def get(self, path: str) -> dict:
        r = self.http.get(path, headers=self.admin)
        if r.status_code >= 400:
            raise SystemExit(f"GET {path}: {r.status_code} {r.text[:300]}")
        return r.json()


def connected_stores(scenario: dict) -> list[dict]:
    """[{store_system_id, party_id, name, complaint, page, stock, pulled}] from the scenario."""
    truth = scenario.get("sim_truth") or {}
    rows = scenario.get("connected_stores")
    if rows is None:      # the short scenario marks connected parties on their entries
        rows = [{"store_system_id": f"ss-{e['party_id']}", "party_id": e["party_id"], "name": e["name"],
                 "complaint": False, "page": True}
                for e in scenario.get("parties", []) + scenario.get("directory", []) if e.get("connected")]
    out = []
    for row in rows:
        t = (truth.get(row["party_id"]) or {}).get("truth") or {}
        received = int(t.get("received") or 25)
        sold = int(t.get("sold") or 0)
        on_shelf_now = max(0, received - sold)
        pulled = max(0, on_shelf_now - int(t.get("on_shelf") or 0)) if t else 0
        out.append({**row, "stock": {"received": received, "sold": sold, "on_shelf": on_shelf_now, "held": 0},
                    "pulled": pulled})
    return out


def register(api: Api, scenario: dict) -> None:
    for s in connected_stores(scenario):
        out = api.post(f"/admin/stores/{s['store_system_id']}",
                       {"store_label": s["name"], "batch_code": BATCH, "stock": s["stock"]}, admin=True)
        st = out["store"]["stock"]
        print(f"connector {s['store_system_id']:<32} {s['name']:<28} {st['on_shelf']} on the shelf, {st['sold']} sold")


def complaints(api: Api, scenario: dict) -> None:
    print("memory:", api.post("/admin/complaints/history", {}, admin=True))
    stores = [s["name"] for s in connected_stores(scenario) if s.get("complaint")] or DEFAULT_COMPLAINT_STORES
    for i, (store, (code, text)) in enumerate(zip(stores, COMPLAINT_TEXTS), start=1):
        out = api.post("/complaints", {"complaint_id": f"c-day0-{i}", "batch_code": BATCH, "bought_at": store,
                                       "reason_code": code, "text": text, "day": 0})
        cls = out["classification"]
        print(f"complaint c-day0-{i} ({store}): {cls.get('defect') or 'no defect: quality lead asked'} "
              f"[{cls.get('method')}, confidence {cls.get('confidence')}, evidence {cls.get('evidence')}]")
        if out.get("signal"):
            sig = out["signal"]
            print(f"  signal {sig['_id']} {sig['status']}: {len(sig['complaints'])} complaints, stores {sig['stores']}")


def approve(api: Api, args) -> None:
    signals = [s for s in api.get("/admin/signals")["signals"] if s["status"] == "proposed" and s["batch_code"] == BATCH]
    if not signals:
        raise SystemExit("no proposed signal on batch " + BATCH)
    body = {"by": args.by, "scenario": args.scenario, "all_sim": args.all_sim}
    if args.recall_id:
        body["recall_id"] = args.recall_id
    out = api.post(f"/admin/signals/{signals[0]['_id']}/approve", body, admin=True)
    print(f"{args.by} approved {out['signal_id']}: recall {out['recall_id']} opened")


def removals(api: Api, scenario: dict, only: str | None) -> None:
    for s in connected_stores(scenario):
        if s.get("page") or (only and s["store_system_id"] != only):
            continue
        sid = s["store_system_id"]
        moved = 0
        for n in range(s["pulled"]):
            out = api.post(f"/stores/{sid}/events", {"event_id": f"rm-{sid}-{n + 1}", "kind": "quarantine",
                                                     "batch_code": BATCH, "qty": 1,
                                                     "text": "staff pulled one tin to quarantine"})
            moved += sum(m["qty"] for m in out.get("moves") or [])
        scan = api.post(f"/stores/{sid}/events", {"event_id": f"rm-{sid}-scan", "kind": "blocked_sale",
                                                  "batch_code": BATCH, "barcode": BARCODE,
                                                  "text": "recalled batch, not for sale"})
        linked = "linked" if scan.get("party_id") else "not linked yet (no party names it)"
        print(f"{s['name']:<28} {s['pulled']} quarantine events, {moved} tins moved to held on the ledger; {linked}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["register", "complaints", "approve", "removals", "run"])
    p.add_argument("--api", default=None, help="API base URL (default API_BASE_URL)")
    p.add_argument("--scenario", default="recall_long")
    p.add_argument("--by", default="operator-1", help="approve: the person approving")
    p.add_argument("--recall-id", default=None)
    p.add_argument("--all-sim", action="store_true")
    p.add_argument("--store", default=None, help="removals: only this store system id")
    args = p.parse_args(argv)
    settings = Settings.from_env()
    api = Api(args.api or settings.api_base_url, settings)
    scenario = load_scenario(args.scenario)
    if args.command in ("register", "run"):
        register(api, scenario)
    if args.command in ("complaints", "run"):
        complaints(api, scenario)
    if args.command == "approve":
        approve(api, args)
    if args.command == "removals":
        removals(api, scenario, args.store)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except httpx.HTTPError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        raise SystemExit(2) from None
