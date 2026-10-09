# Lark → Genie Bot: Per-User OBO Databricks App — Design Spec

- **Date:** 2026-10-07
- **Status:** Design approved; implementation authorized (user waived spec/plan review gates)
- **Approach:** A — Databricks Apps native user authorization + in-memory token store
- **Target:** workspace `<workspace>` (AWS), profile `<profile>`
- **Supersedes:** the continuous-job bot (`src/lark_bot.py` + `resources/lark_bot.job.yml`)

## 1. Context & goal

Today's bot runs as a continuous Databricks **job** under one shared identity (the
job's run-as service principal), so every Lark user queries Genie with the *same*
permissions. This migrates it to a Databricks **App** that calls Genie **on behalf
of each Lark user** (per-user OBO), so Unity Catalog enforces each user's own grants.

**Success criteria:**
- A Lark user binds once (per ~hourly token lifetime), then data questions are
  answered under *their own* Databricks identity.
- Databricks query history attributes the Genie SQL to that user, not a shared SP.
- Two users with different UC grants asking the same question see different data.

## 2. Non-goals (Approach A)

- No custom OAuth app, refresh tokens, or "bind once forever" — that is the
  documented Approach B upgrade.
- No persistent token store — in-memory only; re-bind after app restart.
- No Lark webhook mode — keep the WebSocket long-connection.
- No signed-nonce bind hardening — documented as deferred (see §7).
- No new Genie space — reuse the existing `<workspace>` space.

## 3. Architecture

One **FastAPI** app (served by `uvicorn`), deployed as Databricks App `lark-genie-bot`
to `<workspace>`, running as a **single instance** (the token map is
in-memory and must not be split across replicas). At startup the app launches the
existing **Lark WebSocket client on a daemon thread**; FastAPI serves only the
browser-facing bind routes. One process holds the outbound Lark WS long-connection
*and* answers a few HTTP routes.

**Routes:**
- `GET /` — landing page: what this is + how to bind.
- `GET /bind?open_id=…[&email=…]` — capture point: reads `x-forwarded-access-token`
  (the user's own Databricks token, injected by Apps OBO), stores it keyed by
  `open_id`, renders a "bound ✓" page (scopes + expiry; never the token value).
- `GET /healthz` — liveness.

**Mapping from today's job:** retire the continuous job and the single shared
`WorkspaceClient()`. `ask_genie()` + card/SQL formatting carry over almost verbatim,
parameterized to take a **per-user** client instead of the module-global `_w`.

## 4. Components (small, single-purpose, independently testable)

- **`tokens.py`** — in-memory store. Thread-safe dict `open_id → {access_token,
  expires_at, email}` guarded by a `threading.Lock` (FastAPI request threads write;
  WS worker threads read). API: `put(open_id, token, expires_at, email)`,
  `get(open_id) -> record|None`, `valid(record) -> bool` (expired ~60s before real
  `exp`). Depends on: stdlib only.
- **`genie.py`** — `ask_genie(client, space_id, question) -> dict` (Lark card). The
  existing Conversation-API logic + all `_build_card`/`_pretty_sql`/formatting
  helpers, lifted verbatim except the `WorkspaceClient` is **passed in**. Depends on:
  databricks-sdk (client injected).
- **`bot.py`** — Lark WS handler: builds the Lark client (app creds), registers
  `on_message`, runs the reconnect loop (today's `_run_ws`/`main`). Per message:
  extract `open_id` + text; handle the `绑定` command (always reply a fresh bind
  link); else look up token — valid → per-user `WorkspaceClient(host, token)` →
  `genie.ask_genie(...)` on a worker thread → reply card; missing/expired → reply
  "please (re)bind" + link. Depends on: `tokens`, `genie`, lark-oapi, env (Lark
  creds, host, `APP_BASE_URL`, `GENIE_SPACE_ID`).
- **`app.py`** — FastAPI routes + startup. Launches `bot.run()` on a daemon thread
  (lifespan startup); `/bind` reads the forwarded token, computes expiry, calls
  `tokens.put()`, renders the success page. Depends on: `tokens`, `bot`.

## 5. Data flows

**Bind** (once per ~hour / after restart):
1. User DMs `绑定` (or asks while unbound).
2. `bot` finds no valid token → replies with `{APP_BASE_URL}/bind?open_id=<id>` and
   "open where you're logged into Databricks." (`email` best-effort; see §6.)
3. User opens it → Apps OBO guarantees workspace auth and forwards
   `x-forwarded-access-token`.
4. `/bind` stores `{open_id → token, expiry, email}` (expiry from the token `exp`
   claim, else now+55m) → renders "bound ✓, scopes, expires in N min."

**Ask** (per question):
1. `bot` extracts `open_id` + text; `rec = tokens.get(open_id)`.
2. Missing/expired → reply bind link; stop.
3. Valid → send "正在查询…" ack, then **on a worker thread** (keeps WS keepalive
   unblocked, as today): `WorkspaceClient(host, token=rec.token)` →
   `genie.ask_genie(client, GENIE_SPACE_ID, q)` → reply card. Genie runs SQL **as
   that user**; UC enforces their grants.

## 6. Configuration & secrets

- **Framework/runtime:** FastAPI + uvicorn, Python 3.11. `app.yaml` command
  `["uvicorn","app:app","--host","0.0.0.0","--port","8000"]`; bind the port Apps
  provides (`DATABRICKS_APP_PORT`, default 8000).
- **`requirements.txt`:** `lark-oapi==1.7.3`, `databricks-sdk`, `fastapi`, `uvicorn`.
- **OBO scopes** (declared on the app resource in `databricks.yml`, field
  `user_api_scopes`): `dashboards.genie`, `sql`, `iam.current-user:read`,
  `iam.access-control:read`.
- **Lark creds:** keep in the existing `lark_bot` secret scope
  (`lark_app_id`/`lark_app_secret`); surface to the app as env via `app.yaml`
  `valueFrom`; grant the app's service principal READ on the scope.
- **`GENIE_SPACE_ID`:** bundle variable (default = existing space
  `<genie-space-id>`), passed as app env.
- **`APP_BASE_URL`:** app env = the deployed app URL (from `databricks apps get`),
  used to build bind links. First deploy is two-step: deploy → read URL → set env →
  redeploy.
- **`LARK_REGION`:** `intl` (unchanged).
- **Email (optional):** best-effort fetch of the Lark user's email via the contact
  API for display only; skipped if the Lark contact scope isn't granted. The
  forwarded token is always the authoritative identity.
- **Single instance:** app configured to 1 instance (token map cannot split).

## 7. Error, expiry & security handling

- **Unbound / expired** → `tokens.valid()` fails (incl. 60s margin) → reply bind
  link; no Genie call.
- **401/403 from Genie mid-use** → delete stored token, reply "session expired —
  re-bind." Non-auth errors (query failure, timeout, non-`COMPLETED`) keep today's
  messages and keep the token.
- **`绑定` command** → always returns a fresh link.
- **App restart** → in-memory tokens gone by design; next question returns the bind
  link; WS reconnects via the existing loop.
- **Token hygiene** → never log/print/persist tokens; success page shows scopes +
  expiry only; logs stay at `open_id` + question + outcome.
- **Lark single-connection** → the old continuous job stays **paused**; only this
  single app instance holds the Lark WS (no double-connection).
- **Deferred hardening (not built for A):** the `/bind?open_id=` link is a private
  bearer capability — acceptable over a Lark DM. Hardened version: bot mints a
  short-lived signed `state` nonce (`state → open_id`), included in the link and
  verified at `/bind`, so a forwarded link can't bind someone else's identity.

## 8. Testing

- **Unit (TDD, no Databricks):** `tokens` (put/get/expiry with injected time);
  `genie.ask_genie` against a **fake client** returning canned Conversation-API
  payloads → assert the Lark card structure; `bot` routing → fake message + stub
  store → asserts bind-link-vs-ask-genie branch, and the `绑定` command branch.
- **Local integration (dev path, PAT):** run the app locally; WS connects to Lark;
  Genie via a local PAT (OBO can't be tested locally — `x-forwarded-access-token`
  only exists when deployed). Optional `DEV_USER_TOKEN` env lets `/bind` store a
  token locally; it is ignored when a forwarded token is present.
- **Deployed smoke checklist (the OBO proof):** `/healthz` 200 →
  `/bind?open_id=test` in a Databricks-logged-in browser shows "bound ✓" with the
  `genie` scope → from Lark: `绑定` → bind → ask → answer card → confirm in
  Databricks **query history** that the SQL ran under *your* identity.
- **Demo money-shot:** a second user with different UC grants asks the same
  question and sees different / filtered data.

## 9. Deployment (DABs)

- Keep the project a DABs bundle; replace the `job` resource with an **`app`**
  resource; keep the `dev` target → `<workspace>`.
- `databricks bundle validate --strict -t dev` → `databricks bundle deploy -t dev`
  → `databricks bundle run <app_key> -t dev`.
- First deploy: set `APP_BASE_URL` from `databricks apps get`, redeploy.
- Verify: `databricks apps get lark-genie-bot` (state RUNNING, URL),
  `databricks apps logs lark-genie-bot`.
- Retire: delete `src/lark_bot.py` and `resources/lark_bot.job.yml` from the bundle;
  the paused job already created can be deleted separately once the app is proven.

## 10. Project layout

```
lark-genie-bot/
  databricks.yml          # bundle: vars + dev target + app resource (+ user_api_scopes)
  src/app/
    app.py                # FastAPI: / , /bind , /healthz ; startup → launch WS thread
    bot.py                # Lark WS handler (refactor of lark_bot.py on_message/_run_ws)
    genie.py              # ask_genie(client, space_id, question) -> Lark card
    tokens.py             # in-memory token store
    app.yaml              # uvicorn command + env (valueFrom secrets, GENIE_SPACE_ID, APP_BASE_URL)
    requirements.txt
  tests/
    test_tokens.py
    test_genie.py
    test_bot_routing.py
  docs/superpowers/specs/2026-10-07-lark-genie-obo-app-design.md
  (removed: src/lark_bot.py, resources/lark_bot.job.yml)
```

## 11. To confirm during implementation (not design risks)

- Exact `databricks.yml` **app resource** schema + the `user_api_scopes` field name
  (verify via `databricks apps manifest` / bundle schema).
- `app.yaml` **secret** `valueFrom` syntax for referencing `lark_bot` scope keys.
- Whether an injected app-URL env var exists (else keep `APP_BASE_URL` explicit).
- Forwarded-token `exp` parsing (JWT) vs the ~55-min default.
- Confirm the App keeps the process (and WS thread) running continuously once started.
