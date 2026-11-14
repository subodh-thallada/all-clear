"""Two nodes in one test process: ours (the parent) and Summit Foods' (the child).

Both databases sit on the test server (the session's scratch database and a
second one named after it, dropped with it). The two FastAPI apps are called
in-process through TestClient (an httpx.Client): the parent's worker reaches
Summit through `child_http`, Summit's reporter reaches us through
`parent_http`. No network.
"""
from __future__ import annotations

import copy
from dataclasses import replace
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from levela.api.main import create_app
from levela.ledger.transfers import edge_id, ledger_fields, v2_fields
from levela.memory.embed import get_embedder
from levela.node import child, links

SECRET = "test-link-secret"
RECALL = "r-1"
SUMMIT = "summit-foods"
EAST = "dist-east"
LINK = "ln-summit"
PRODUCT = "Maple Farm milk powder 800 g"
BARCODE = "0 62345 18803 5"


def recall_doc(units: int = 120, **over) -> dict:
    return {"_id": RECALL, "product": PRODUCT, "barcode": BARCODE, "batch_code": "B-4471",
            "affected_batches": ["B-4471"], "action": "return", "barcode_verified": True, "defect": "seal_fault",
            "checks": None, "source": "manual", "source_signal": None, "units_total": units, "open_units": units,
            "status": "open", **over}


def _party(pid, name, *, type_, tier, channel, shipped_in, **over) -> dict:
    doc = {"_id": f"{RECALL}:{pid}", "recall_id": RECALL, "party_id": pid, "name": name, "type": type_,
           "tier": tier, "channel": channel, "state": "pending", **ledger_fields(shipped_in, {"batch_code": "B-4471"}),
           **v2_fields(tier=tier, found_via="shipment" if tier == 1 else "order_system", next_action_at=None)}
    doc.update(over)
    return doc


def seed_parent(db, *, units: int = 120) -> None:
    """Our ledger at day 2: East forwarded `units` tins to Summit Foods (its
    account list), the edge awaits Summit's count, and a link matches the account."""
    db.recalls.insert_one(recall_doc(units))
    db.parties.insert_many([
        _party(EAST, "Distributor East", type_="distributor", tier=1, channel="order_system", shipped_in=units,
               forwarded=units, unresolved=0, order_system="east", state="claimed"),
        _party(SUMMIT, "Summit Foods", type_="store", tier=2, channel="agent", shipped_in=0, link_id=LINK,
               sub=links.empty_sub(), account="EAST-SUMMIT", distributor=EAST, phone="555-0400"),
    ])
    db.edges.insert_one({"_id": edge_id(RECALL, EAST, SUMMIT), "recall_id": RECALL, "from_party": EAST,
                         "to_party": SUMMIT, "sender_qty": units, "recipient_qty": None, "corroborated_qty": 0,
                         "disputed_qty": 0, "state": "awaiting_recipient"})
    links.ensure_link(db, links.link_doc(link_id=LINK, role="parent", peer_node="summit", peer_url="http://summit",
                                         match={"system": "east", "account": "EAST-SUMMIT"}, party_id=SUMMIT,
                                         status="active"))


class CountingClient:
    """An httpx-like client that counts requests by (method, path) and can be stopped."""

    def __init__(self, inner):
        self.inner, self.seen, self.stopped = inner, [], False

    def _go(self, method, url, **kw):
        path = httpx.URL(url).path
        self.seen.append((method, path))
        if self.stopped:
            raise httpx.ConnectError("node stopped", request=httpx.Request(method, url))
        return getattr(self.inner, method.lower())(url, **kw)

    def get(self, url, **kw):
        return self._go("GET", url, **kw)

    def post(self, url, **kw):
        return self._go("POST", url, **kw)

    def count(self, method, prefix):
        return sum(1 for m, p in self.seen if m == method and p.startswith(prefix))


def scenario(received: int | None = None) -> dict:
    scn = copy.deepcopy(child.load_scenario())
    if received is not None:
        scn["received"] = received
    return scn


def make_nodes(client, settings, db, summit_db, *, scn=None):
    parent_settings = replace(settings, node_id="maple-farm", agent_link_secret=SECRET, scheduler_enabled=False)
    child_settings = replace(settings, db_name=summit_db.name, node_id="summit", parent_url="http://parent",
                             agent_link_secret=SECRET, scheduler_enabled=False)
    parent_app = create_app(parent_settings, client=client)
    child_app = create_app(child_settings, client=client)
    scn = scn or scenario()
    child_app.state.node_scenario = scn
    embedder = get_embedder(child_settings)
    child.seed_node(summit_db, scn, embedder, parent_url="http://parent")
    child_http = CountingClient(TestClient(child_app, base_url="http://summit"))
    parent_http = CountingClient(TestClient(parent_app, base_url="http://parent"))
    ctx = SimpleNamespace(db=db, client=client, settings=parent_settings, http=child_http, failpoints=None)
    return SimpleNamespace(parent_app=parent_app, child_app=child_app, parent_settings=parent_settings,
                           child_settings=child_settings, child_http=child_http, parent_http=parent_http, ctx=ctx,
                           scn=scn, embedder=embedder, db=db, summit_db=summit_db, client=client)


def summit_party(db) -> dict:
    return db.parties.find_one({"recall_id": RECALL, "party_id": SUMMIT})


def child_recall_id() -> str:
    return child.child_recall_id("summit", RECALL)
