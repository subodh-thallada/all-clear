"""The go/no-go before a live run: `python -m levela preflight`.

Every check is a PASS or FAIL line; the command exits 1 when any fails.

  the deployment is a replica set (transactions and change streams need one)
  every validator and index matches levela.db.schema (run setup-db if not)
  a clean slate: no demo recall, no episode left from a rehearsal, no live attempt
  the vector index is queryable, at the embedder's dimensions
  the query embedder is the model the stored episodes were embedded with
  the two demo phones are E.164 numbers
"""
from __future__ import annotations

from pymongo.errors import OperationFailure

from .config import DEMO_RECALL_IDS, LIVE_ATTEMPT_STATES
from .db.schema import ALL_VALIDATORS, INDEXES, VECTOR_INDEX, describe
from .db.seed import E164
from .memory.embed import EMBED_ERRORS, check_embedder, embedder_dims


class Report:
    """Named checks with PASS / FAIL / NOTE, rendered as plain text."""

    def __init__(self, title: str):
        self.title, self.rows = title, []

    def check(self, name: str, ok, detail="") -> bool:
        self.rows.append((name, bool(ok), str(detail)))
        return bool(ok)

    def note(self, name: str, detail="") -> None:
        self.rows.append((name, None, str(detail)))

    @property
    def failed(self) -> list:
        return [r for r in self.rows if r[1] is False]

    def render(self) -> str:
        mark = {True: "PASS", False: "FAIL", None: "NOTE"}
        lines = [self.title, "-" * len(self.title)]
        lines += [f"  {mark[ok]}  {name}" + (f"  [{detail}]" if detail else "") for name, ok, detail in self.rows]
        done = [r for r in self.rows if r[1] is not None]
        lines.append(f"  {len(done) - len(self.failed)}/{len(done)} checks passed"
                     + ("" if not self.failed else f", {len(self.failed)} FAILED"))
        return "\n".join(lines)


def check_schema(db, rep: Report) -> None:
    have = describe(db)
    for name, wanted in ALL_VALIDATORS.items():
        got = (have.get(name) or {}).get("validator")
        rep.check(f"validator on {name} matches the schema", got == wanted,
                  "" if got == wanted else "missing or different; run setup-db")
    for coll, keys, opts in INDEXES:
        ix = ((have.get(coll) or {}).get("indexes") or {}).get(opts["name"])
        ok = (ix is not None and list(ix["key"].items()) == [(k, d) for k, d in keys]
              and bool(ix.get("unique")) == bool(opts.get("unique"))
              and ix.get("partialFilterExpression") == opts.get("partialFilterExpression"))
        rep.check(f"index {coll}.{opts['name']}", ok)


def _vector_index(db, rep: Report, embedder) -> None:
    try:
        info = list(db.episodes.list_search_indexes(VECTOR_INDEX))
    except OperationFailure as exc:
        rep.check(f"vector index {VECTOR_INDEX} is queryable", False,
                  f"search index commands unavailable here ({exc.code}); memory scores locally")
        return
    ok = bool(info) and bool(info[0].get("queryable"))
    if not rep.check(f"vector index {VECTOR_INDEX} is queryable", ok) or embedder is None:
        return
    dims = next(f["numDimensions"] for f in info[0]["latestDefinition"]["fields"] if f["type"] == "vector")
    try:
        want = embedder_dims(embedder)
    except EMBED_ERRORS as exc:
        rep.check("vector index dimensions match the embedder", False, f"embedder unreachable: {exc}")
        return
    rep.check("vector index dimensions match the embedder", dims == want, f"index {dims}, embedder {want}")


def preflight(client, db, rep: Report, env, embedder=None) -> None:
    """Run every check into `rep`. `env` holds the demo phones."""
    hello = client.admin.command("hello")
    rep.check("deployment is a replica set (transactions and change streams need one)",
              bool(hello.get("setName")) or hello.get("msg") == "isdbgrid")
    rep.note("server version", client.server_info()["version"])
    check_schema(db, rep)
    rep.check("no demo recall exists yet (clean slate)",
              db.recalls.count_documents({"_id": {"$in": DEMO_RECALL_IDS}}) == 0, "run reset")
    left = db.episodes.count_documents({})
    rep.check("no episode left over from a rehearsal", left == 0,
              f"{left} found; run reset or run 1 will reach Corner Mart at second zero")
    rep.check("no live attempt anywhere",
              db.attempts.count_documents({"state": {"$in": list(LIVE_ATTEMPT_STATES)}}) == 0)
    _vector_index(db, rep, embedder)
    if embedder is not None:
        ok, detail = check_embedder(db, embedder)
        rep.check("query embedder matches the stored episodes", ok, detail)
    harbour, corner = env.get("PHONE_HARBOUR", ""), env.get("PHONE_CORNER", "")
    rep.check("PHONE_HARBOUR is an E.164 number", bool(E164.match(harbour)))
    rep.check("PHONE_CORNER is an E.164 number", bool(E164.match(corner)))
