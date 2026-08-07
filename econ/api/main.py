import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from econ.api.routers import admin, auth, entities, goods, intents, markets, needs, parcels, production, proposals, scripts, tech, ticks, transactions

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


@app.get("/healthz", tags=["health"])
def healthz():
    return {"status": "ok"}
