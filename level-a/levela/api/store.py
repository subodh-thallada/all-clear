"""Store records endpoints (spec "Store records" > "Endpoints added").

  GET  /store/{id}                    the simulated store page ("SIMULATED" across the top)
  POST /store/{id}/press              the page's buttons (admin token as ?key=, like the wall's)
  POST /stores/{id}/events            a connected store's event (header X-Tool-Secret: the
                                      shared secret of the simulated parties)
  POST /complaints                    the company's complaint form (header X-Tool-Secret)
  POST /admin/signals/{id}/approve    a person approves a proposed signal; the recall opens
  GET  /admin/signals                 the signals with their evidence, for the operator page
  POST /admin/stores/{id}             set a store's block or stock numbers for a test
  POST /admin/stores/{id}/block       ask a linked store's till to block the product
  POST /admin/complaints/{id}/finding a person's finding on a complaint
  POST /admin/complaints/history      seed the confirmed past complaints (memory)
  POST /admin/plant/{recall_id}/check read the plant's answers again
  POST /admin/plant/{recall_id}/confirm  a person confirms a batch or the action the plant named
"""
from __future__ import annotations

import html
import logging
import secrets
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from ..monitoring import classify, link, plant, signals
from ..monitoring.store_events import StoreEventError, post_event
from .form import form_body
from .deps import jsonable, require_admin, require_tool_secret, secret_matches

log = logging.getLogger("levela.api.store")
router = APIRouter()

PRESS_ACTIONS = ("scan", "quarantine", "return")


def _embedder(st):
    from ..memory.embed import get_embedder
    return get_embedder(st.settings)


def _store_or_404(db, store_id: str) -> dict:
    store = db.store_systems.find_one({"_id": store_id})
    if store is None:
        raise HTTPException(status_code=404, detail=f"unknown store {store_id}")
    return store


def _http(exc: Exception, status: int = 400) -> HTTPException:
    code = getattr(exc, "code", "bad_request")
    return HTTPException(status_code=404 if code.startswith("unknown") else status,
                         detail={"code": code, "message": getattr(exc, "message", str(exc))})


# ---- the store's connector ------------------------------------------------------------

def apply_event(st, store_id: str, event: dict) -> dict:
    """Link the connector if a party of an open recall now carries its store
    name, then post the event (one transaction)."""
    try:
        link.try_link(st.db, store_id, client=st.client)
    except StoreEventError:
        raise
    except Exception as exc:  # noqa: BLE001 - an event is never lost to a link failure
        log.warning("linking %s failed: %s", store_id, exc)
    return post_event(st.db, store_id, event, client=st.client)


@router.post("/stores/{store_id}/events", dependencies=[Depends(require_tool_secret)])
def store_event(store_id: str, event: dict, request: Request) -> dict:
    st = request.app.state
    _store_or_404(st.db, store_id)
    try:
        return jsonable(apply_event(st, store_id, event))
    except StoreEventError as exc:
        raise _http(exc) from None


# ---- complaints -----------------------------------------------------------------------

def handle_complaint(st, payload: dict) -> dict:
    """Store, classify, count. Returns the complaint, its classification and
    the signal it raised or joined (if any)."""
    db = st.db
    stored = classify.receive(db, payload)
    cls = classify.classify(db, stored["_id"], embedder=_embedder(st), decider=classify.make_decider(st.settings))
    changed = signals.evaluate(db, batch_code=stored["batch_code"]) if cls.get("defect") else []
    after = db.complaints.find_one({"_id": stored["_id"]})
    sig = db.signals.find_one({"_id": after.get("signal_id")}) if after.get("signal_id") else None
    return {"complaint": after, "classification": cls, "duplicate": stored.get("duplicate", False),
            "signal": sig, "signals_changed": [s["_id"] for s in changed]}


@router.post("/complaints", dependencies=[Depends(require_tool_secret)])
def complaint(payload: dict, request: Request) -> dict:
    try:
        return jsonable(handle_complaint(request.app.state, payload))
    except classify.ComplaintError as exc:
        raise _http(exc) from None


# ---- admin ----------------------------------------------------------------------------

class Approve(BaseModel):
    by: str
    scenario: str = "recall_1"
    recall_id: str | None = None
    all_sim: bool = False
    mode: str | None = None
    memory: bool | None = None
    seconds_per_sim_hour: float | None = None
    hold_slice: bool | None = None


@router.post("/admin/signals/{signal_id}/approve", dependencies=[Depends(require_admin)])
def approve(signal_id: str, body: Approve, request: Request) -> dict:
    from ..db import seed
    st = request.app.state
    if body.scenario not in seed.list_scenarios():
        raise HTTPException(status_code=400, detail=f"unknown scenario {body.scenario!r}")
    memory = st.settings.memory_enabled if body.memory is None else body.memory
    kwargs = {k: v for k, v in {"all_sim": body.all_sim, "mode": body.mode, "memory": memory,
                                "seconds_per_sim_hour": body.seconds_per_sim_hour,
                                "hold_slice": body.hold_slice}.items() if v is not None}
    try:
        out = signals.approve(st.client, st.db, signal_id, by=body.by, scenario=seed.load_scenario(body.scenario),
                              recall_id=body.recall_id, **kwargs)
    except signals.SignalError as exc:
        raise _http(exc, 409) from None
    return jsonable(out)


@router.get("/admin/signals", dependencies=[Depends(require_admin)])
def list_signals(request: Request) -> dict:
    db = request.app.state.db
    rows = list(db.signals.find().sort("_id", 1))
    for s in rows:
        s["evidence"] = list(db.complaints.find({"_id": {"$in": s.get("complaints") or []}},
                                                {"bought_at": 1, "text": 1, "classification": 1, "day": 1}))
    return jsonable({"signals": rows})


class StoreSet(BaseModel):
    store_label: str | None = None
    batch_code: str | None = None
    blocked: bool | None = None
    stock: dict | None = None
    recall_id: str | None = None
    party_id: str | None = None


@router.post("/admin/stores/{store_id}", dependencies=[Depends(require_admin)])
def admin_store(store_id: str, body: StoreSet, request: Request) -> dict:
    st = request.app.state
    try:
        out = link.set_store(st.db, store_id, blocked=body.blocked, stock=body.stock, store_label=body.store_label,
                             batch_code=body.batch_code)
        linked = None
        if body.recall_id and body.party_id:
            linked = link.link_store(st.db, store_id, body.recall_id, body.party_id, client=st.client)
    except StoreEventError as exc:
        raise _http(exc) from None
    return jsonable({"store": st.db.store_systems.find_one({"_id": store_id}) if linked else out, "linked": linked})


@router.post("/admin/stores/{store_id}/block", dependencies=[Depends(require_admin)])
def admin_block(store_id: str, request: Request) -> dict:
    try:
        return jsonable(link.request_block(request.app.state.db, store_id))
    except StoreEventError as exc:
        raise _http(exc) from None


class Finding(BaseModel):
    defect: str
    by: str


@router.post("/admin/complaints/{complaint_id}/finding", dependencies=[Depends(require_admin)])
def finding(complaint_id: str, body: Finding, request: Request) -> dict:
    st = request.app.state
    try:
        cls = classify.record_finding(st.db, complaint_id, defect=body.defect, by=body.by, embedder=_embedder(st))
    except classify.ComplaintError as exc:
        raise _http(exc) from None
    changed = signals.evaluate(st.db, batch_code=st.db.complaints.find_one({"_id": complaint_id})["batch_code"])
    return jsonable({"classification": cls, "signals_changed": [s["_id"] for s in changed]})


@router.post("/admin/complaints/history", dependencies=[Depends(require_admin)])
def history(request: Request) -> dict:
    st = request.app.state
    return {"seeded": classify.seed_history(st.db, _embedder(st))}


@router.post("/admin/plant/{recall_id}/check", dependencies=[Depends(require_admin)])
def plant_check(recall_id: str, request: Request) -> dict:
    return jsonable(plant.apply_answers(request.app.state.db, recall_id))


class Confirm(BaseModel):
    by: str
    batch: str | None = None
    action: str | None = None


@router.post("/admin/plant/{recall_id}/confirm", dependencies=[Depends(require_admin)])
def plant_confirm(recall_id: str, body: Confirm, request: Request) -> dict:
    try:
        return jsonable(plant.confirm(request.app.state.db, recall_id, by=body.by, batch=body.batch,
                                      action=body.action))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


# ---- the simulated store page -----------------------------------------------------------

def _key_ok(st, key: str | None) -> bool:
    return secret_matches(key, st.settings.admin_token)


def press_event(st, store: dict, action: str) -> dict:
    """What one button posts. Scan: `sale` while the product is not blocked,
    `blocked_sale` once it is."""
    if action not in PRESS_ACTIONS:
        raise HTTPException(status_code=400, detail=f"action is one of {', '.join(PRESS_ACTIONS)}")
    recall = st.db.recalls.find_one({"_id": store.get("recall_id")}) if store.get("recall_id") else None
    barcode = (recall or {}).get("barcode") or "0 62345 18803 5"
    base = {"event_id": f"page-{secrets.token_hex(5)}", "batch_code": store.get("batch_code") or "B-4471", "qty": 1}
    if action == "scan":
        if store.get("blocked"):
            return {**base, "kind": "blocked_sale", "barcode": barcode, "text": "recalled batch, not for sale"}
        return {**base, "kind": "sale", "barcode": barcode}
    if action == "quarantine":
        return {**base, "kind": "quarantine", "text": "staff moved one tin to quarantine"}
    return {**base, "kind": "customer_return", "text": "a shopper returned one tin"}


@router.post("/store/{store_id}/press")
async def press(store_id: str, request: Request):
    st = request.app.state
    key = request.query_params.get("key")
    if not _key_ok(st, key):
        raise HTTPException(status_code=401, detail="bad or missing ?key= (the admin token)")
    # Parsed with urllib.parse, as the fallback form does (levela/api/form.py):
    # python-multipart is not a dependency. The body is a few bytes ("action=...").
    body = await form_body(request)
    if body is None:
        raise HTTPException(status_code=413, detail="request body too large")
    fields = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
    action = str((fields.get("action") or [""])[0] or request.query_params.get("action") or "")
    store = _store_or_404(st.db, store_id)
    import anyio
    try:
        result = await anyio.to_thread.run_sync(lambda: apply_event(st, store_id, press_event(st, store, action)))
    except StoreEventError as exc:
        raise _http(exc) from None
    if "application/json" in (request.headers.get("accept") or ""):
        return jsonable(result)
    return RedirectResponse(f"/store/{quote(store_id)}?key={quote(key or '')}", status_code=303)


def _fmt(dt) -> str:
    return dt.strftime("%H:%M:%S UTC") if hasattr(dt, "strftime") else ""


def render_page(store: dict, recall: dict | None, party: dict | None, key: str | None) -> str:
    e = html.escape
    stock = store.get("stock") or {}
    product = (recall or {}).get("product") or store.get("product") or "Maple Farm milk powder 800 g"
    last = store.get("last_event") or {}
    till = "product BLOCKED" if store.get("blocked") else "product on sale"
    last_line = "none yet"
    if last:
        last_line = f"{e(last.get('kind', ''))}"
        if last.get("text"):
            last_line += f" &nbsp; &ldquo;{e(last['text'])}&rdquo;"
        last_line += f" &nbsp; {e(_fmt(last.get('at')))}"
        if last.get("breach"):
            last_line = f"<span class='bad'>BREACH: {last_line}</span>"
    ledger = ""
    if party:
        ledger = (f"<p class='small'>Recall {e(str(recall.get('_id')))}: party {e(party['party_id'])}, ledger "
                  f"{party.get('on_shelf', 0)} on the shelf, {party.get('held', 0)} held, {party.get('sold', 0)} "
                  f"sold; evidence held_by_system {party.get('held_by_system', 0)}, system_on_shelf "
                  f"{party.get('system_on_shelf', 0)}</p>")
    else:
        ledger = "<p class='small'>Not linked to a recall yet.</p>"
    if key:
        action = f"/store/{quote(store['_id'])}/press?key={quote(key)}"
        buttons = "".join(
            f"<form method='post' action='{e(action)}'><input type='hidden' name='action' value='{a}'>"
            f"<button type='submit'>{label}</button></form>"
            for a, label in (("scan", "Scan a tin at the till"), ("quarantine", "Move one tin to quarantine"),
                             ("return", "Customer return")))
    else:
        buttons = "<p class='small'>Open with ?key=&lt;admin token&gt; to press the buttons.</p>"
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>SIMULATED STORE: {e(store['store_label'])}</title>
<meta http-equiv="refresh" content="5">
<style>
body {{ font-family: ui-monospace, Menlo, Consolas, monospace; margin: 0; background: #111; color: #eee; }}
.banner {{ background: #b00; color: #fff; font-weight: bold; padding: 8px 16px; letter-spacing: .2em; }}
main {{ padding: 16px; }} .row {{ font-size: 20px; margin: 10px 0; }}
.bad {{ color: #ff5c5c; font-weight: bold; }} .small {{ color: #999; font-size: 13px; }}
form {{ display: inline-block; margin-right: 12px; }}
button {{ font: inherit; font-size: 18px; padding: 10px 16px; cursor: pointer; }}
</style></head><body>
<div class="banner">SIMULATED STORE &nbsp; not a real till or stock system</div>
<main>
<div class="row">{e(store['store_label'])} &nbsp; {e(product)} &nbsp; Batch {e(store.get('batch_code') or '')}</div>
<div class="row">Stock record: {int(stock.get('on_shelf', 0))} on the shelf, {int(stock.get('held', 0))} held
 &nbsp; Till: {"<span class='bad'>" + till + "</span>" if store.get('blocked') else till}</div>
<div class="row">Last event: {last_line}</div>
<div class="row">{buttons}</div>
{ledger}
</main></body></html>"""


@router.get("/store/{store_id}", response_class=HTMLResponse)
def store_page(store_id: str, request: Request) -> HTMLResponse:
    st = request.app.state
    store = _store_or_404(st.db, store_id)
    key = request.query_params.get("key")
    recall = st.db.recalls.find_one({"_id": store.get("recall_id")}) if store.get("recall_id") else None
    party = (st.db.parties.find_one({"recall_id": store["recall_id"], "party_id": store.get("party_id")})
             if recall and store.get("party_id") else None)
    return HTMLResponse(render_page(store, recall, party, key if _key_ok(st, key) else None))
