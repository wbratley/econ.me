import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from econ.api.routers import admin, auth, catalog, entities, epochs, goods, governance, intents, join, leaderboard, markets, mcp, needs, parcels, production, proposals, rounds, scripts, tech, ticks, transactions
from econ.api.routers.activity import router as activity_router

app = FastAPI(title="econ.me API", version="0.1.0")

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
