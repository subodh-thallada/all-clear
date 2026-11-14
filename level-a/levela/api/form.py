"""The fallback form: GET/POST /form/{token}, and its QR code.

A party (or a judge holding a phone) can give its counts here instead of on a
call. The claim takes exactly the voice tool's path (tools.book_claim), so
the same numbers produce the same Atlas rejection, the same acceptance and
the same feed lines.

Attempts: a submission uses the party's live attempt when there is one (the
agent may be on the phone with the party right now); otherwise it opens a
"form" attempt, which is confirmed and connected from the start because
there is no call to be in doubt about. A form attempt opened here is
completed when its submission has been handled, whatever the outcome: one
left live after a rejection (a judge who types 60 and walks away, or a
submission that failed to book) held the party's one-live-attempt slot for
good, so the party could never be called again, and a later submission with
the same numbers reused its dedupe scope and got the stale rejection back.
The party's live call attempt, when the form used it, is the worker's to
close.

Fields: a party that some sender reported forwarding tins to is asked for
its receipt whatever its tier (a tier 1 party can receive a transfer too),
and a party several senders shipped to also picks which one the tins came
from. The delivery note is asked when the call asked for it or when memory
flagged the party (V2 / DESIGN §10.4: an over-reporter must give it on every
path, the form included).

Evidence: every submission is logged in tool_events (kind "form") before it
is booked, and an accepted one is the read-back of the party's outstanding
send_form_link operations, which move to verified (V2 §6 "Verify": a form
link counts when a submission arrives with the party's token).

The page is self-contained (inline CSS, no scripts, no external assets) and
the body is parsed with urllib.parse, because python-multipart is not a
dependency. The body is read through a capped reader (MAX_BODY): the form is
public, and an oversized body is refused without being held in memory.
"""
from __future__ import annotations

import html
import io
import json
import logging
from urllib.parse import parse_qs

import segno
from bson.errors import InvalidDocument
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from pymongo.errors import PyMongoError

from .. import feed
from ..orchestrator import attempts
from . import evidence
from .deps import form_url, read_capped, sim_hour, utcnow
from ..ledger.claims import CHECK_FIELDS
from .tools import book_claim, describe_counts, receives_transfers

log = logging.getLogger("levela.api.form")
router = APIRouter()

MAX_BODY = 16 * 1024
NUMBER_FIELDS = ("sold", "on_shelf", "held", "returned", "forwarded_qty", "received_qty")
COUNT_KEYS = ("sold", "on_shelf", "held", "returned", "forwarded_qty", "forwarded_to_name", "delivery_note")
BUSY_TEXT = "Someone else is updating this party right now. Please submit again."
UNRECORDED_TEXT = "Your counts could not be recorded just now. Nothing was saved. Please submit again."

# Human wording for rejections the party can fix (the agent's wording is for a model).
REJECTION_TEXT = {
    "delivery_note_required": "Enter the delivery note number from your paperwork, then submit again.",
    "delivery_note_mismatch": "That delivery note does not match the shipment record. Check it and submit again.",
    "no_inbound_edge": ("No party has reported sending tins to you yet, so a receipt cannot be recorded. "
                        "The recall team will contact you again."),
    "receipt_sender_ambiguous": ("Tins reached you from more than one party. Choose the party these tins came "
                                 "from, then submit again."),
    "forward_missing_name": "Enter the name of the store the tins went to.",
    "recall_closed": "This recall is already closed.",
    "held_needs_evidence": ("A tin counts as held only with the batch code read from under one tin and a till "
                            "that refused the sale. Enter both, or count the tins as on the shelf."),
    "batch_mismatch": ("That is not the recalled batch. Read the batch code printed under one tin again, or count "
                       "the tins as on the shelf."),
    "block_not_confirmed": ("A tin counts as held only once the till refuses the sale when one is scanned. Count "
                            "the tins as on the shelf until it does."),
    "sold_needs_till": ("A sold count must come from the till. Open the item sales report on the till and enter "
                        "the number it shows."),
    "stock_record_on_shelf": ("Your store's own stock record still shows tins on the shelf, and it outranks this "
                              "form. Count them as on the shelf until staff move them to quarantine."),
    "bad_answer": "One of the check answers could not be read. Check it and submit again.",
}


async def form_body(request: Request) -> bytes | None:
    """The submission's bytes, or None when it is larger than MAX_BODY (the
    page then says so with a 413); never more than MAX_BODY is held."""
    return await read_capped(request, MAX_BODY)


# ---- routes -------------------------------------------------------------------

@router.get("/form/{token}", response_class=HTMLResponse)
def show_form(token: str, request: Request) -> HTMLResponse:
    db = request.app.state.db
    found = _load(db, token)
    if found is None:
        return _not_found()
    party, recall = found
    live = attempts.live_attempt(db, party["recall_id"], party["party_id"])
    return HTMLResponse(render_page(party, recall, need_note=_needs_note(party, live), senders=_senders(db, party),
                                    receipt=receives_transfers(db, party)))


@router.post("/form/{token}", response_class=HTMLResponse)
def submit_form(token: str, request: Request, body: bytes | None = Depends(form_body)) -> HTMLResponse:
    st = request.app.state
    db = st.db
    found = _load(db, token)
    if found is None:
        return _not_found()
    party, recall = found
    if body is None:
        return HTMLResponse(render_page(party, recall, error="That submission is too large."), status_code=413)
    values = parse_form(body)
    senders, receipt = _senders(db, party), receives_transfers(db, party)
    if recall["status"] != "open":
        return HTMLResponse(render_page(party, recall))
    raw = raw_from_form(values, party, receipt=receipt)
    if not any(k in raw for k in NUMBER_FIELDS):
        live = attempts.live_attempt(db, party["recall_id"], party["party_id"])
        return HTMLResponse(render_page(party, recall, values=values, need_note=_needs_note(party, live),
                                        senders=senders, receipt=receipt, error="Enter at least one number."))
    attempt = form_attempt(db, party)
    if attempt is None:
        return HTMLResponse(render_page(party, recall, values=values, senders=senders, receipt=receipt,
                                        error=BUSY_TEXT))
    need_note = _needs_note(party, attempt)
    # The ledger logs the submission as this claim's evidence (tool_events kind
    # "form", keyed by the dedupe key) before booking it, so it is not logged here.
    feed.emit(db, "form_claim", f"{party['name']} submitted counts through the web form",
              recall_id=party["recall_id"], party_id=party["party_id"], attempt_id=attempt["_id"], system="form",
              detail={"raw": raw, "attempt_id": attempt["_id"], "attempt_channel": attempt["channel"]})
    result = None
    try:
        result, _ = book_claim(st.client, db, attempt, raw, source="form", require_delivery_note=need_note)
    except (PyMongoError, OverflowError, InvalidDocument):
        log.exception("form submission for %s could not be booked", party["party_id"])
        return HTMLResponse(render_page(party, recall, values=values, need_note=need_note, senders=senders,
                                        receipt=receipt, error=UNRECORDED_TEXT), status_code=503)
    finally:
        if attempt["channel"] == "form":
            _close_form_attempt(db, attempt, result)
    if result["accepted"]:
        evidence.verify_form_links(db, party, result["claim_id"], sim_hour(st, party["recall_id"]))
    booked = db.parties.find_one({"_id": party["_id"]}) or party
    return HTMLResponse(render_page(booked, recall, values=values, need_note=need_note, senders=senders,
                                    receipt=receipt, result=result, raw=raw))


@router.get("/form/{token}/qr.svg")
def qr_code(token: str, request: Request) -> Response:
    """A QR code of the form's public URL, for the wall or a printed card."""
    st = request.app.state
    if st.db.parties.find_one({"form_token": token}, {"_id": 1}) is None:
        return Response("unknown party link", status_code=404, media_type="text/plain")
    buf = io.BytesIO()
    segno.make(form_url(st.settings, token), error="m").save(buf, kind="svg", scale=8, border=2, xmldecl=False)
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-cache"})


# ---- data ---------------------------------------------------------------------

def _load(db, token: str):
    party = db.parties.find_one({"form_token": token})
    if party is None:
        return None
    recall = db.recalls.find_one({"_id": party["recall_id"]}, {"directory": 0})
    return (party, recall) if recall else None


def _needs_note(party: dict, attempt: dict | None) -> bool:
    """The delivery note is required when the call asked for it, or when
    memory flagged the party (parties.memory.actions, the flag the worker sets
    from an over_reported episode). The form attempt has no call context, so
    reading the call's flag alone let a flagged party skip the note simply by
    using the form."""
    asked = bool(attempt) and (attempt.get("context") or {}).get("ask_delivery_note") == "yes"
    flagged = any(a.get("action") == "ask_delivery_note"
                  for a in ((party.get("memory") or {}).get("actions") or []) if isinstance(a, dict))
    return asked or flagged


def _senders(db, party: dict) -> list[str]:
    """The names of the parties that reported forwarding tins to this party,
    whatever its tier, so the form can ask about them by name (and ask which
    one, when there are several)."""
    ids = sorted(e["from_party"] for e in db.edges.find(
        {"recall_id": party["recall_id"], "to_party": party["party_id"]}, {"from_party": 1}))
    if not ids:
        return []
    names = {s["party_id"]: s["name"] for s in db.parties.find(
        {"recall_id": party["recall_id"], "party_id": {"$in": ids}}, {"party_id": 1, "name": 1})}
    return [names.get(sid, sid) for sid in ids]


def parse_form(body: bytes) -> dict:
    """application/x-www-form-urlencoded -> {field: stripped first value}."""
    pairs = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
    return {k: v[0].strip() for k, v in pairs.items() if v}


def raw_from_form(values: dict, party: dict, *, receipt: bool | None = None) -> dict:
    """The claim as the party typed it. Blank fields are left out (the ledger
    treats a missing bucket as 0, and a missing check keeps its earlier
    answer). The receipt (and its sender) is read only from a party the form
    asked for one (`receipt`, see tools.receives_transfers; without it, a
    party below tier 1): a tier 1 party that received a transfer is asked too,
    or its receipt was dropped and its true counts refused as an over-count.
    The three checks are read from a store only."""
    keys = list(COUNT_KEYS)
    asks_receipt = party.get("tier") != 1 if receipt is None else receipt
    if asks_receipt:
        keys += ["received_qty", "received_from"]
    if party.get("type") == "store":
        keys += list(CHECK_FIELDS)
    return {k: values[k] for k in keys if values.get(k)}


def _close_form_attempt(db, attempt: dict, result: dict | None) -> None:
    """Complete a form attempt opened for this submission, whatever came of
    it: the submission is the whole contact. A failure to close is logged and
    left to the next submission, which reuses the live attempt and closes it."""
    if result is None:
        reason = "form submission not recorded"
    elif result["accepted"]:
        reason = "form claim accepted"
    else:
        reason = f"form claim rejected: {result.get('code')}"
    try:
        attempts.complete(db, attempt["_id"], "completed", reason=reason)
    except PyMongoError:
        log.exception("could not complete form attempt %s", attempt["_id"])


def form_attempt(db, party: dict) -> dict | None:
    """The party's live attempt, or a new confirmed, connected form attempt.
    None only if a concurrent submission raced this one and already finished."""
    rid, sid = party["recall_id"], party["party_id"]
    live = attempts.live_attempt(db, rid, sid)
    if live:
        return live
    try:
        attempt = attempts.open_attempt(db, recall_id=rid, party_id=sid, channel="form", purpose="form",
                                        extra={"dial_confirmed": True})
    except attempts.AttemptRefused as exc:
        log.info("form attempt for %s refused, using the live one: %s", sid, exc.message)
        return exc.existing or attempts.live_attempt(db, rid, sid)
    attempts.transition(db, attempt["_id"], "connected")
    return {**attempt, "state": "connected"}


# ---- rendering ----------------------------------------------------------------

CSS = """
:root{--bg:#f4f6f8;--card:#fff;--ink:#14181d;--muted:#545e69;--line:#c3ccd5;--accent:#1d4ed8;--on-accent:#fff;
--ok-bg:#e6f5eb;--ok-ink:#0f5a2b;--ok-line:#2e9554;--bad-bg:#fcebeb;--bad-ink:#86191a;--bad-line:#d14343;
--hazard:#9b1c1c;--focus:#d97706}
@media (prefers-color-scheme:dark){:root{--bg:#0e1115;--card:#171b21;--ink:#eef1f4;--muted:#a7b0ba;--line:#3b444e;
--accent:#8fb0ff;--on-accent:#0b1222;--ok-bg:#0f2819;--ok-ink:#a5e6bb;--ok-line:#2e9554;--bad-bg:#2c1414;
--bad-ink:#ffbdbd;--bad-line:#d14343;--hazard:#ffb3b3}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:18px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:34rem;margin:0 auto;padding:20px 16px 48px}
.eyebrow{margin:0;color:var(--muted);font-size:15px;letter-spacing:.02em}
h1{margin:4px 0 12px;font-size:28px;line-height:1.2}
h2{margin:0 0 8px;font-size:22px}
.facts{margin:0;padding:12px 16px;background:var(--card);border:1px solid var(--line);border-radius:12px}
.facts div{display:flex;justify-content:space-between;gap:16px;padding:6px 0}
.facts div+div{border-top:1px solid var(--line)}
dt{color:var(--muted)}
dd{margin:0;font-weight:600;text-align:right}
.hazard dd{color:var(--hazard)}
form{margin-top:8px}
fieldset{border:0;margin:24px 0 0;padding:0;min-width:0}
legend{font-size:20px;font-weight:700;padding:0;margin-bottom:4px}
label{display:block;font-weight:600;margin:16px 0 6px}
.hint{display:block;font-weight:400;color:var(--muted);font-size:15px}
input,select{display:block;width:100%;min-height:56px;font:inherit;font-size:22px;padding:10px 14px;
border:2px solid var(--line);border-radius:10px;background:var(--card);color:var(--ink)}
input:focus-visible,select:focus-visible,button:focus-visible,summary:focus-visible{outline:3px solid var(--focus);
outline-offset:2px}
button{display:block;width:100%;min-height:60px;margin-top:28px;font:inherit;font-size:20px;font-weight:700;
border:0;border-radius:12px;background:var(--accent);color:var(--on-accent);cursor:pointer}
.result{border:2px solid;border-radius:12px;padding:14px 16px;margin:20px 0 0}
.ok{background:var(--ok-bg);border-color:var(--ok-line);color:var(--ok-ink)}
.bad{background:var(--bad-bg);border-color:var(--bad-line);color:var(--bad-ink)}
.result p{margin:8px 0 0}
.numbers{display:flex;gap:12px;margin:12px 0 4px}
.numbers div{flex:1;text-align:center;padding:10px;border-radius:10px;background:var(--card);color:var(--ink)}
.numbers b{display:block;font-size:40px;line-height:1.1}
.mono,pre{font:14px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
pre{margin:8px 0 0;white-space:pre-wrap;word-break:break-word;background:var(--card);color:var(--ink);
padding:10px 12px;border:1px solid var(--line);border-radius:8px;overflow-x:auto}
summary{cursor:pointer;min-height:44px;display:flex;align-items:center;font-weight:600;margin-top:8px}
.booked{margin:10px 0 0;padding:0;list-style:none;display:flex;flex-wrap:wrap;gap:6px 14px;font-size:16px}
.foot{margin-top:28px;color:var(--muted);font-size:15px}
"""


def _e(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _page(title: str, body: str) -> str:
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<meta name=\"robots\" content=\"noindex\">"
            f"<title>{_e(title)}</title><style>{CSS}</style></head><body><main>{body}</main></body></html>")


def _not_found() -> HTMLResponse:
    body = ("<h1>This link is not valid</h1><p>The recall link may have been mistyped, or the recall has been "
            "reset. Ask the recall team for a new link.</p>")
    return HTMLResponse(_page("Link not valid", body), status_code=404)


def render_page(party: dict, recall: dict, *, values: dict | None = None, need_note: bool = False,
                senders: list[str] | None = None, receipt: bool | None = None, result: dict | None = None,
                raw: dict | None = None, error: str | None = None) -> str:
    """The whole page. `senders` names the parties that forwarded tins here;
    `receipt` says whether to ask for a receipt (default: a party below tier
    1, or any party with a sender)."""
    senders = senders or []
    if receipt is None:
        receipt = party.get("tier") != 1 or bool(senders)
    parts = [_header(party, recall)]
    if error:
        parts.append(f"<section class=\"result bad\" role=\"alert\"><p>{_e(error)}</p></section>")
    if result is not None:
        parts.append(_result(result, party, raw or {}))
    if recall["status"] != "open":
        parts.append("<section class=\"result ok\" role=\"status\"><h2>All clear</h2>"
                     "<p>Every tin of the batch is found and off the shelf. Thank you.</p></section>")
    else:
        parts.append(_form(party, recall, values or {}, need_note, senders, receipt))
    parts.append("<p class=\"foot\">Your numbers are checked by the database against the shipment record. "
                 "Nothing is saved unless every tin adds up.</p>")
    return _page(f"{party['name']} - recall of batch {recall['batch_code']}", "".join(parts))


def _header(party: dict, recall: dict) -> str:
    facts = [("Product", recall.get("product")), ("Company", recall.get("company")),
             ("Barcode", recall.get("barcode")), ("Batch", recall.get("batch_code"))]
    rows = "".join(f"<div><dt>{_e(k)}</dt><dd>{_e(v)}</dd></div>" for k, v in facts if v)
    rows += f"<div class=\"hazard\"><dt>Hazard</dt><dd>{_e(recall.get('hazard'))}</dd></div>"
    return (f"<header><p class=\"eyebrow\">All Clear: tin recall {_e(recall['_id'])}</p>"
            f"<h1>{_e(party['name'])}</h1><dl class=\"facts\">{rows}</dl></header>")


def _field(name: str, label: str, hint: str, values: dict, *, numeric: bool = True) -> str:
    mode = " inputmode=\"numeric\"" if numeric else ""
    return (f"<label for=\"{name}\">{_e(label)}<span class=\"hint\">{_e(hint)}</span></label>"
            f"<input id=\"{name}\" name=\"{name}\"{mode} autocomplete=\"off\" value=\"{_e(values.get(name))}\">")


def _choice(name: str, label: str, hint: str, options: list[str], values: dict, *, prompt: str = "Choose a party") -> str:
    """A select of fixed options (no scripts needed), keeping what was chosen."""
    chosen = values.get(name)
    items = f"<option value=\"\">{_e(prompt)}</option>" + "".join(
        f"<option value=\"{_e(o)}\"{' selected' if o == chosen else ''}>{_e(o)}</option>" for o in options)
    return (f"<label for=\"{name}\">{_e(label)}<span class=\"hint\">{_e(hint)}</span></label>"
            f"<select id=\"{name}\" name=\"{name}\">{items}</select>")


def _plural(unit: str) -> str:
    return f"{unit}es" if unit.endswith(("s", "x", "ch", "sh")) else f"{unit}s"


def _form(party: dict, recall: dict, values: dict, need_note: bool, senders: list[str], receipt: bool) -> str:
    unit = _plural(recall.get("unit") or "tin")
    fields = []
    if need_note:
        fields.append(_field("delivery_note", "Delivery note number", "Printed on the delivery paperwork",
                             values, numeric=False))
    if receipt and len(senders) > 1:
        # One receipt per submission: the ledger books it on that sender's transfer.
        fields.append(_choice("received_from", "Received from", "Several parties sent tins to you: pick one, "
                              "enter what came from it, and submit once for each party", senders, values))
        fields.append(_field("received_qty", f"{unit.capitalize()} received", "From the party chosen above", values))
    elif receipt:
        hint = f"From {senders[0]}" if senders else "From the party that sent them to you"
        fields.append(_field("received_qty", f"{unit.capitalize()} received", hint, values))
    fields += [
        _field("sold", "Sold", "Sold to shoppers since the delivery, as the till's item sales report shows", values),
        _field("on_shelf", "On the shelf", "Still out for sale", values),
        _field("held", "Held", "Pulled from sale and kept apart", values),
        _field("returned", "Returned", "Sent back to the company", values),
    ]
    forward = ("<fieldset><legend>Passed to another store</legend>"
               + _field("forwarded_qty", f"How many {unit}", "Leave blank if none", values)
               + _field("forwarded_to_name", "Store", "Their name", values, numeric=False)
               + "</fieldset>")
    checks = ""
    if party.get("type") == "store":
        checks = ("<fieldset><legend>Checks</legend>"
                  + _field("batch_seen", "Batch code", "Printed under one tin, for example B-4471", values,
                           numeric=False)
                  + _choice("sold_source", "Where the sold count came from",
                            "Open the item sales report on the till", ["till report", "estimate"], values,
                            prompt="Choose one")
                  + _choice("block_test", "Till test", "Scan one tin at the till: did the till refuse the sale?",
                            ["yes", "no"], values, prompt="Choose yes or no")
                  + "</fieldset>")
    return (f"<form method=\"post\" novalidate><fieldset><legend>Count the {_e(unit)} of batch "
            f"{_e(recall['batch_code'])}</legend>{''.join(fields)}</fieldset>{forward}{checks}"
            "<button type=\"submit\">Submit counts</button></form>")


def _result(result: dict, party: dict, raw: dict) -> str:
    if result["accepted"]:
        return _accepted(result, party, raw)
    return _rejected(result)


def _accepted(result: dict, party: dict, raw: dict) -> str:
    said = ("These counts were already recorded." if result.get("duplicate")
            else f"Recorded: {describe_counts(raw)}.")
    booked = [("Sold", "sold"), ("On the shelf", "on_shelf"), ("Held", "held"),
              ("Returned", "returned"), ("Passed on", "forwarded"), ("Not located", "unresolved")]
    items = "".join(f"<li>{_e(label)} <b>{_e(party.get(key))}</b></li>" for label, key in booked)
    return ("<section class=\"result ok\" role=\"status\"><h2>Accepted</h2>"
            f"<p>{_e(said)}</p><p>Atlas now holds for {_e(party['name'])}:</p><ul class=\"booked\">{items}</ul>"
            "</section>")


def _rejected(result: dict) -> str:
    err = result.get("atlas_error")
    title = "Rejected by Atlas" if err else "Not recorded"
    parts = [f"<section class=\"result bad\" role=\"alert\"><h2>{title}</h2>"]
    if result.get("code") == "over_count":
        parts.append(f"<div class=\"numbers\"><div><b>{_e(result['reported_total'])}</b>reported</div>"
                     f"<div><b>{_e(result['shipped_in'])}</b>shipped</div></div>"
                     f"<p>Your counts add up to {_e(result['reported_total'])}, but the shipment record shows "
                     f"{_e(result['shipped_in'])} delivered to this party. Check and submit again.</p>")
    else:
        reason = str(result.get("reason") or "the claim was refused")
        text = REJECTION_TEXT.get(result.get("code"))
        parts.append(f"<p>{_e(text or reason[:1].upper() + reason[1:] + '.')}</p>")
        if text and result.get("values"):
            parts.append(f"<p class=\"mono\">{_e(reason)}</p>")
    if result.get("duplicate"):
        parts.append("<p>These exact counts were already checked and refused.</p>")
    if err:
        parts.append(f"<p class=\"mono\">{_e(err.get('code'))} {_e(err.get('codeName'))}: {_e(err.get('errmsg'))}"
                     "</p><details><summary>Raw Atlas error (errInfo)</summary>"
                     f"<pre>{_e(json.dumps(err.get('errInfo'), indent=2, default=str))}</pre></details>")
    parts.append("</section>")
    return "".join(parts)
