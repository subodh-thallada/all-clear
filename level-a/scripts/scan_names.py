"""Spec test 48: the buyer list stays home.

    python scripts/scan_names.py --uri "$ATLAS_URI" --db levela [--summit scenarios/summit.json] [--recall r-1]

Reads every buyer contact and every staff phone number in Summit Foods'
scenario file and looks for each one in every document of OUR database (the
parent node's). Any hit is a leak. Phone numbers are also matched without
their separators. With --recall it also checks that our recall is closed
only if every party answered by another node's agent reported its
sub-recall closed.

Exit status 0 when clean, 1 on a leak or a recall closed too early.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SUMMIT_FILE = ROOT / "scenarios" / "summit.json"


def needles(scn: dict) -> list[str]:
    """Every buyer contact and staff phone number in Summit's file. The
    business line (on East's account list) is not secret and not included."""
    found = [b["contact"] for b in scn.get("buyers", [])]
    found.append(scn["depot"]["staff"]["phone"])
    found += [s["manager"]["phone"] for s in scn.get("stores", [])]
    return sorted(set(found))


def _patterns(needle: str) -> list[re.Pattern]:
    pats = [re.compile(re.escape(needle))]
    digits = re.sub(r"\D", "", needle)
    if len(digits) >= 7 and digits != needle:
        pats.append(re.compile(rf"(?<!\d){digits}(?!\d)"))
        pats.append(re.compile(rf"(?<!\d){digits[:3]}[ .]{digits[3:]}(?!\d)"))
    return pats


def scan(db, scn: dict) -> dict:
    """{"clean", "scanned", "hits": [{collection, _id, needle}]} over every
    collection of `db`."""
    compiled = [(n, _patterns(n)) for n in needles(scn)]
    hits, scanned = [], 0
    for name in sorted(db.list_collection_names()):
        if name.startswith("system."):
            continue
        for doc in db[name].find({}):
            scanned += 1
            text = json.dumps(doc, default=str)
            for needle, pats in compiled:
                if any(p.search(text) for p in pats):
                    hits.append({"collection": name, "_id": str(doc.get("_id")), "needle": needle})
    return {"clean": not hits, "scanned": scanned, "needles": len(compiled), "hits": hits}


def closed_too_early(db, recall_id: str) -> list[str]:
    """Agent parties whose sub-recall was not reported closed while our recall is closed."""
    recall = db.recalls.find_one({"_id": recall_id})
    if recall is None or recall.get("status") != "closed":
        return []
    return [p["party_id"] for p in db.parties.find({"recall_id": recall_id, "channel": "agent"})
            if (p.get("sub") or {}).get("state") != "closed"]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Spec test 48: no Summit buyer contact or staff phone in our database")
    parser.add_argument("--uri", default=None, help="MongoDB URI (default: ATLAS_URI from the environment / .env)")
    parser.add_argument("--db", default=None, help="our database (default: ATLAS_DB or levela)")
    parser.add_argument("--summit", default=str(SUMMIT_FILE))
    parser.add_argument("--recall", default=None, help="also check this recall closed only after its sub-recalls")
    args = parser.parse_args(argv)
    from pymongo import MongoClient

    from levela.config import Settings
    s = Settings.from_env()
    client = MongoClient(args.uri or s.atlas_uri, tz_aware=True, serverSelectionTimeoutMS=15000)
    db = client[args.db or s.db_name]
    scn = json.loads(Path(args.summit).read_text(encoding="utf-8"))
    res = scan(db, scn)
    early = closed_too_early(db, args.recall) if args.recall else []
    for h in res["hits"]:
        print(f"LEAK  {h['collection']}/{h['_id']}: {h['needle']}")
    for pid in early:
        print(f"EARLY recall {args.recall} is closed but {pid} never reported its sub-recall closed")
    print(f"scanned {res['scanned']} documents of {db.name} for {res['needles']} contacts and staff numbers: "
          + ("clean" if res["clean"] else f"{len(res['hits'])} leak(s)"))
    return 0 if res["clean"] and not early else 1


if __name__ == "__main__":
    raise SystemExit(main())
