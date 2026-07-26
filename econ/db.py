"""Product-owned database configuration. The engine core (econengine) is
session-in only — it never creates engines or sessions; each consumer owns
its own DB config and migrations."""
import os

from sqlalchemy import create_engine

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///econ.db")

# ctx.query.* callbacks run on the Lua script's worker thread while the main
# thread blocks, so the sqlite3 same-thread check must be off. Access is
# still serialized — only one thread touches the connection at a time.
_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=_connect_args)
