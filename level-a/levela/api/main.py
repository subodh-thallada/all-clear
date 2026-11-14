"""The API server: create_app() wires every router to one Mongo database.

    uvicorn levela.api.main:create_app --factory      (or: python -m levela api)

The API receives every claim (voice tool, simulator, fallback form, reports),
the post-call webhooks, the admin actions and the wall's polling. It also
hosts the simulated phone network, so a simulated call outlives a killed
worker the way a real call does, and the scheduler that checks the four
invariants once per simulated hour.

Endpoints are plain `def` functions: pymongo is synchronous, and FastAPI runs
sync endpoints in its thread pool.
"""
from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pymongo.errors import PyMongoError

from .. import __version__
from ..config import Settings
from ..db.client import get_client
from . import admin, admin_run, form, hooks, order_admin, sim, tools, wall
from .scheduler import Scheduler

log = logging.getLogger("levela.api")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """The scheduler thread runs while the server serves. It is tied to the
    lifespan, not to create_app itself, so building an app (as every test
    does) starts no background thread."""
    st = app.state
    if st.settings.scheduler_enabled:
        st.scheduler.start()
        # The simulated phone network also schedules party-initiated events in
        # simulated time (revisions, form submissions, the day-8 rep report).
        # They wait in Atlas; starting the network here resumes them after an
        # API restart instead of on the next simulated call.
        try:
            started = sim.network(st)
            if hasattr(started, "start"):
                started.start()
        except Exception as exc:  # noqa: BLE001 - the API must serve claims even without the simulator
            log.warning("simulator network did not start: %s", exc)
    try:
        yield
    finally:
        st.scheduler.stop()
        if st.sim is not None and hasattr(st.sim, "stop"):
            st.sim.stop()
        if st.owns_http:
            st.http.close()


def create_app(settings: Settings | None = None, *, sim_network=None, client=None, http=None) -> FastAPI:
    """Build the app. `sim_network` replaces the simulator (tests pass a stub);
    `client` replaces the MongoClient (tests share theirs); `http` replaces
    the httpx client used to reach the mock order systems (tests pass one with a
    MockTransport)."""
    if not logging.getLogger().handlers:
        # Under `uvicorn --factory` nothing configures the root logger, and
        # the levela.* lines (feed, claims) would be dropped.
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s: %(message)s")
    settings = settings or Settings.from_env()
    client = client or get_client(settings, appname="levela-api")

    app = FastAPI(title="All Clear", version=__version__, lifespan=_lifespan,
                  description="All Clear: claims, post-call webhooks, fallback form, admin, the screen.")
    _cap_request_bodies(app)
    st = app.state
    st.settings = settings
    st.client = client
    st.db = client[settings.db_name]
    st.sim = sim_network
    st.sim_lock = threading.Lock()
    st.wall_cache = wall.StateCache(wall.CACHE_TTL_S)
    st.owns_http = http is None
    st.http = http or order_admin.LazyHttp()
    st.scheduler = Scheduler(settings, client, st.db, http=st.http)

    for module in (tools, hooks, form, admin, admin_run, sim, wall):
        app.include_router(module.router)
    wall.mount_static(app)

    @app.get("/healthz")
    def healthz(request: Request) -> JSONResponse:
        state = request.app.state
        try:
            state.db.command("ping")
            ok = True
        except PyMongoError as exc:
            log.warning("health check: database unreachable: %s", exc)
            ok = False
        return JSONResponse({"ok": ok, "db": state.settings.db_name, "version": __version__},
                            status_code=200 if ok else 503)

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/wall")

    log.info("API ready on database %s (public base %s)", settings.db_name, settings.webhook_base)
    return app


# A request body larger than this is refused with 413 before any route runs.
# FastAPI parses a Pydantic body before a route's auth dependency, so without
# this an anonymous client could make the server buffer a huge body on
# /admin/* or /sim/*. The post-call webhook may carry base64 audio.
BODY_CAPS = (("/hooks/", 64 * 1024 * 1024), ("/", 1024 * 1024))


def _body_cap(path: str) -> int:
    return next(cap for prefix, cap in BODY_CAPS if path.startswith(prefix))


def _cap_request_bodies(app: FastAPI) -> None:
    @app.middleware("http")
    async def cap_body(request: Request, call_next):
        length = request.headers.get("content-length")
        if length is not None:
            try:
                too_big = int(length) > _body_cap(request.url.path)
            except ValueError:
                return JSONResponse({"detail": "bad Content-Length"}, status_code=400)
            if too_big:
                return JSONResponse({"detail": "request body too large"}, status_code=413)
        return await call_next(request)
