"""The world server app: one FastAPI instance over one world.db.

Round-clock wiring (game.md §9): routes own consent and operator
advances; when the deadline backstop is armed (ECON_ROUND_DEADLINE_S >
0) a lifespan background task closes rounds nobody closed -- the
always-on host (M2a): a seat that never shows costs the world one
deadline period, not a hang.
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from econ.api import events
from econ.api.routers import admin, auth, catalog, entities, epochs, goods, governance, intents, join, leaderboard, markets, mcp, needs, parcels, production, proposals, rounds, scripts, tech, ticks, transactions
from econ.api.routers.activity import router as activity_router

log = logging.getLogger("econ.api.scheduler")

#: How often the deadline backstop checks the round clock.
SCHEDULER_POLL_S = 5.0


def _deadline_poll_once() -> dict | None:
    """One scheduler tick: close the round if its time is up. Own short
    session -- SQLite's single writer means no long-lived transaction may
    squat between polls; commits the anchor-write too (a round first seen
    without one). Returns the summary when it closed a round."""
    from sqlalchemy.orm import Session

    from econ.db import engine as db_engine
    from econ.api.rounds import maybe_auto_advance
    from fastapi.encoders import jsonable_encoder

    # Tests override the engine on app.state; production has no such
    # attribute and rides the configured DATABASE_URL engine.
    engine = getattr(app.state, "_test_engine", None) or db_engine
    with Session(engine) as session:
        summary = maybe_auto_advance(session)
        session.commit()
        return jsonable_encoder(summary) if summary else None


async def _deadline_scheduler() -> None:
    """Armed only when the backstop env is set at startup; dies with the
    app. A poll that loses the write race (SQLite busy timeout vs a
    concurrent consent-resolve) rolls back and retries next tick."""
    while True:
        await asyncio.sleep(SCHEDULER_POLL_S)
        try:
            summary = await asyncio.to_thread(_deadline_poll_once)
        except Exception:  # noqa: BLE001 -- the clock must survive its ticks
            log.warning("round-deadline poll failed", exc_info=True)
            continue
        if summary:
            events.publish_round_closed(summary)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from econ.api.rounds import round_deadline_s

    task = None
    if round_deadline_s() > 0:
        task = asyncio.create_task(_deadline_scheduler())
    yield
    if task is not None:
        task.cancel()


app = FastAPI(title="econ.me API", version="0.1.0", lifespan=lifespan)

app.add_middleware(SessionMiddleware, secret_key=os.environ.get("SECRET_KEY", "dev-secret-key-change-in-production"))
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(join.router)
app.include_router(rounds.router)
app.include_router(epochs.router)
app.include_router(governance.router)
app.include_router(leaderboard.router)
app.include_router(mcp.router)
app.include_router(entities.router)
app.include_router(transactions.router)
app.include_router(admin.router)
app.include_router(scripts.router)
app.include_router(proposals.router)
app.include_router(ticks.router)
app.include_router(markets.router)
app.include_router(production.router)
app.include_router(parcels.router)
app.include_router(goods.router)
app.include_router(needs.router)
app.include_router(tech.router)
app.include_router(intents.router)
app.include_router(catalog.router)
app.include_router(activity_router)


@app.get("/healthz", tags=["health"])
def healthz():
    return {"status": "ok"}
