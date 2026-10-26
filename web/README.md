# Living world client

The reference web client for `docs/world-layer.md`: a pixel-art
`tile2d.v1` spectator/player view over the platform's public scene
projection. It renders generated terrain, roads, places,
parcels/facilities/deposits, animated actors, work/travel/walk states, and
public speech. Pure spectating works without an entity; the same account
can also join through the ordinary onboarding endpoint, then possess a
placed ACTIVE non-fixed individual for local walking and engine-backed
speech on an armed world clock.

## Run it

Start the API first:

```sh
.venv/bin/uvicorn econ.api.main:app --reload
```

Then start the web client:

```sh
cd web
npm install
npm run dev
```

Open the printed Vite URL (default <http://localhost:5173>). The dev server
proxies `/world`, `/join`, and `/rounds` to <http://127.0.0.1:8000>.

To watch a `nim_run` world instead of a standalone API, point the proxy at
that run's **world server port** (not its dashboard port):

```sh
cd web
ECON_API_PROXY_TARGET=http://127.0.0.1:8917 npm run dev
```

The Vite server binds the LAN by default, so a phone can open
`http://<this-host>:5173` while the proxy keeps the API server address
server-side. Treat pasted bearer tokens as account credentials.

Sign in with email from the connect panel: the client calls
`POST /auth/magic/request`, the server emails a short-lived link (with no
SMTP configured it logs the link to the server console instead — the dev
loop), and clicking it lands back on the client with a one-shot exchange
code (`/?code=…`) that the page redeems via `POST /auth/magic/exchange`
for the access token — the token itself never rides a URL. Identity is
the email address: a magic sign-in lands on the same account an OAuth
login with that address created. Delivery is configured with
`ECON_SMTP_HOST`/`ECON_SMTP_PORT`/`ECON_SMTP_USER`/`ECON_SMTP_PASSWORD`/
`ECON_SMTP_FROM`/`ECON_SMTP_STARTTLS`, the link TTL with
`ECON_MAGIC_LINK_EXPIRE_MINUTES` (default 15), and the post-verify
redirect target with `ECON_WEB_CLIENT_URL` (default
<http://localhost:5173>).

Pasting an account bearer token into the client still works. A token identifies the
spectator and does **not** require an entity. If that account owns a placed
ACTIVE non-fixed individual, the Walk & talk panel offers possession; if
not, it offers **Join the world**. Joining calls the ordinary `POST /join`
path, creates this account's founder from the pack's `join.config`, then
refreshes the world bootstrap so the founder can be possessed. On a
development login, the OAuth callback's `access_token` value is the token
to paste; a `nim_run --browser-user` token works the same way. Tokens are
kept in `sessionStorage`, never in the URL.

World speech and engine actions wait for the world clock: launch the run
with `--world-tick-seconds` (see `experiments/agent/README.md`) if you
want browser `say` and in-progress joining rather than read-only watching.

## Build and test

```sh
cd web
npm run build
npm run test:browser
```

`test:browser` launches the system Chromium via `puppeteer-core` (no browser
download), serves the static build with a tiny mock world API, and verifies
phone layout connection, joining, rendered canvas variety, WebSocket scene
deltas and reconnect, possession/move/say commands, speech, follow mode,
and desktop resize. Set `CHROME_BIN` if Chromium lives somewhere unusual.

The static build lands in `web/dist` (ignored). To host it apart from the
API, build with the API origin:

```sh
VITE_API_BASE=https://api.example.com npm run build
```

The client remains protocol-first: bootstrap supplies the geometry
descriptor, cache key, scene URL, chunk URL template, SSE URL, WebSocket URL,
and the account's possessable entities; no game content or economic state is
baked into the bundle. The same bearer token authorizes the optional
`POST /join` onboarding call. `/world/stream` authenticates with a first JSON
message (`{"type":"auth","token":...}`), never a token in the URL, then
sends a hello snapshot, field-level scene deltas, presence deltas, and
command results. W2 client commands are `possess`, `release`, `move`, and
`say`; all carry a per-session sequence and are revalidated server-side.
