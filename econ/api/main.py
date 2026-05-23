import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from econ.api.routers import admin, auth, entities, transactions

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


@app.get("/healthz", tags=["health"])
def healthz():
    return {"status": "ok"}
