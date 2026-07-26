---
name: verify
description: Build, launch, and drive econ.me's FastAPI server against a scratch DB to verify changes end-to-end at the API surface.
---

# Verifying econ.me changes

The surface is the FastAPI app (`econ.api.main:app`). Auth is JWT bearer;
mint tokens directly instead of driving OAuth.

## Launch against a scratch DB

Run everything from the repo root with the venv python (`.venv/bin/python`;
bare `python` does not exist). `DATABASE_URL` picks the DB (defaults to
`sqlite:///econ.db` — the gitignored dev DB, don't verify against it).

```bash
SCRATCH=<scratchpad dir>
# seed schema + users, mint tokens (prints ADMIN/USER JWTs)
DATABASE_URL="sqlite:///$SCRATCH/verify.db" .venv/bin/python - <<'EOF'
from sqlalchemy.orm import Session
from econ.models import Base, engine, User
from econ.api.auth import create_token
Base.metadata.create_all(engine)
with Session(engine) as s:
    s.add_all([
        User(id="u-admin", email="admin@x", name="Admin", provider="test", provider_id="1", is_admin=True),
        User(id="u-user", email="user@x", name="User", provider="test", provider_id="2"),
    ])
    s.commit()
print("ADMIN", create_token("u-admin", "admin@x", True))
print("USER", create_token("u-user", "user@x", False))
EOF
DATABASE_URL="sqlite:///$SCRATCH/verify.db" .venv/bin/python -m uvicorn econ.api.main:app --port 8321 &
curl -s http://127.0.0.1:8321/healthz   # {"status":"ok"}
```

Note `Base.metadata.create_all` builds the schema from models, bypassing
Alembic. To exercise a migration, run `.venv/bin/python -m alembic upgrade
head` (and `downgrade -1` / re-upgrade) — alembic.ini pins
`sqlite:///econ.db`, so that runs against the gitignored dev DB.

## Driving flows

- All routes need `Authorization: Bearer <token>`; `/admin/*` needs the
  admin one.
- Typical loop: admin POSTs definitions (`/admin/recipes`,
  `/admin/technologies`, `/admin/needs`, `/admin/markets`), user POSTs
  `/entities` then acts (`/processes`, orders), admin grants holdings via
  `POST /admin/holdings {entity_id, symbol, delta}`, ticks advance via
  `POST /admin/ticks` (response carries the tick's `events` — the best
  observability for engine passes).
- Script-driven behaviour: insert a `Script` row (BEHAVIOUR, entity_id
  set) and run ticks; script errors surface as `script_error` events.

## Gotchas

- A process started before tick N completes at tick N+duration; duration 0
  completes at start (visible immediately in `/entities/{id}/holdings`).
- SQLite doesn't enforce VARCHAR lengths, so symbol-width changes can't be
  proven by inserts — check the DDL (`sqlite3 file .schema table`) instead.
