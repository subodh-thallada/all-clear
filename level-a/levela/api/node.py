"""Agent-to-agent endpoints: /agent/recalls, /agent/recalls/{key}, /agent/reports, /agent/health, /admin/peer/over-report.

Every /agent request carries X-AllClear-Signature (levela.node.links): an
HMAC-SHA256 of the timestamp and the exact body bytes with the pairing's
secret, no older than 5 minutes. It is checked before anything is parsed;
a failure is a 401 and a document in `traces`.

  POST /agent/recalls          child   take a sub-recall, once per key
  GET  /agent/recalls/{key}    child   read-back for the handoff
  POST /agent/reports          parent  a report, booked as a claim (source agent)
  GET  /agent/health           both    last sequence numbers and times;
                                       ?ask=report asks the child for a report
  POST /admin/peer/over-report child   the next report claims more held than
                                       its records show (admin token)

These are plain endpoints of our own, not the Agent2Agent protocol.
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from ..node import child, links
from ..node.report import book_report
from .deps import jsonable, read_capped, require_admin

router = APIRouter()
MAX_AGENT_BODY = 64 * 1024


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _secrets(state) -> list[str]:
    out = [links.link_secret(state.settings, link["peer_node"]) for link in state.db.links.find({}, {"peer_node": 1})]
    out.append(state.settings.agent_link_secret)
    return [s for i, s in enumerate(out) if s and s not in out[:i]]


def _trace_refusal(state, request: Request, reason: str) -> None:
    tid = f"agent-auth:{secrets.token_hex(6)}"
    try:
        state.db.traces.insert_one({"_id": f"{tid}:001", "run_id": tid, "seq": 1, "kind": "result",
                                    "tool": "agent_signature", "recall_id": None, "party_id": None,
                                    "output": {"refused": True, "path": request.url.path, "reason": reason,
                                               "node": state.settings.node_id},
                                    "at": utcnow()})
    except Exception:  # noqa: BLE001 - a lost trace must not turn a 401 into a 500
        pass


async def _signed_body(request: Request) -> bytes:
    raw = await read_capped(request, MAX_AGENT_BODY)
    if raw is None:
        raise HTTPException(status_code=413, detail="request body too large")
    state = request.app.state
    header = request.headers.get(links.HEADER)
    reason = "no link secret configured on this node"
    for secret in _secrets(state):
        reason = links.check(raw, header, secret)
        if reason is None:
            return raw
    _trace_refusal(state, request, reason)
    raise HTTPException(status_code=401, detail=f"bad {links.HEADER}: {reason}")


def _json(raw: bytes) -> dict:
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="body must be JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    return body


def _scenario(state) -> dict:
    scn = getattr(state, "node_scenario", None)
    if scn is None:
        scn = child.load_scenario()
        state.node_scenario = scn
    return scn


def _embedder(state):
    emb = getattr(state, "node_embedder", None)
    if emb is None:
        from ..memory.embed import get_embedder
        emb = get_embedder(state.settings)
        state.node_embedder = emb
    return emb


@router.post("/agent/recalls")
def take_sub_recall(request: Request, raw: bytes = Depends(_signed_body)) -> JSONResponse:
    state = request.app.state
    body = _json(raw)
    scn = _scenario(state)
    if state.db.episodes.find_one({"kind": "item"}, {"_id": 1}) is None:
        child.seed_node(state.db, scn, _embedder(state), parent_url=state.settings.parent_url)
    try:
        out = child.take_sub_recall(state.client, state.db, scn, _embedder(state), body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return JSONResponse(jsonable(out))


@router.get("/agent/recalls/{key}")
def read_sub_recall(key: str, request: Request, raw: bytes = Depends(_signed_body)) -> JSONResponse:
    found = child.read_back(request.app.state.db, key)
    if found is None:
        raise HTTPException(status_code=404, detail=f"no sub-recall for key {key}")
    return JSONResponse(jsonable(found))


@router.post("/agent/reports")
def receive_report(request: Request, raw: bytes = Depends(_signed_body)) -> JSONResponse:
    state = request.app.state
    report = _json(raw)
    link = links.get(state.db, str(report.get("link") or ""))
    if link is None or link.get("role") != "parent" or not link.get("party_id"):
        raise HTTPException(status_code=404, detail=f"no link {report.get('link')!r} to a party on this node")
    party = state.db.parties.find_one({"party_id": link["party_id"], "link_id": link["_id"],
                                       **({"recall_id": report["recall"]} if report.get("recall") else {})})
    if party is None:
        raise HTTPException(status_code=404, detail=f"no party for link {link['_id']}")
    out = book_report(state.db, recall_id=party["recall_id"], party_id=party["party_id"], report=report)
    return JSONResponse(jsonable(out))


@router.get("/agent/health")
def health(request: Request, ask: str | None = Query(None), raw: bytes = Depends(_signed_body)) -> JSONResponse:
    state = request.app.state
    if ask == "report":
        state.db.links.update_many({"role": "child"}, {"$set": {"report_requested": True}})
    rows = [{"link_id": l["_id"], "role": l["role"], "peer_node": l["peer_node"], "status": l.get("status"),
             "last_seq_sent": l.get("last_seq_sent"), "last_seq_accepted": l.get("last_seq_accepted"),
             "last_seen_at": l.get("last_seen_at")} for l in state.db.links.find()]
    return JSONResponse(jsonable({"ok": True, "node_id": state.settings.node_id, "links": rows,
                                  "report_requested": ask == "report"}))


@router.post("/admin/peer/over-report", dependencies=[Depends(require_admin)])
def over_report(request: Request, by: int = Query(2, ge=1, le=1000)) -> JSONResponse:
    res = request.app.state.db.links.update_many({"role": "child"}, {"$set": {"over_report_next": by}})
    return JSONResponse({"ok": True, "links": res.matched_count, "over_report_by": by,
                         "note": "the next report claims more held tins than this node's records show"})
