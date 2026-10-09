# Lark → Genie Bot (per-user OBO)

A Databricks App that answers data questions asked in **Lark/Feishu** with
**Databricks Genie**, running **as the asking user** (on-behalf-of authorization), so
Unity Catalog enforces each person's own grants and query history attributes the SQL
to them.

Clone it, point it at your Genie space and Lark bot, and deploy it to any Databricks
workspace. Nothing in the repo is tied to a particular workspace: per-deployment
values are bundle variables or app resources, and the app discovers its own URL.

## How it works

The pivotal constraint: Databricks Apps hand you a user's token
(`x-forwarded-access-token`) **only on an inbound browser request**, and it's a
short-lived (~1h) access token with no reusable refresh token. A Lark WebSocket
event carries no such token — so the app **captures the token on a one-time browser
"bind", stores it in memory keyed by the Lark `open_id`, and reuses it** from the WS
thread until it expires.

```
BIND  (once per ~hour / after restart)        ASK  (every question)
──────────────────────────────────────        ─────────────────────
Lark user ─DM "绑定"─▶ bot                      Lark user ─question─▶ bot (WS thread)
  bot replies: <APP_URL>/bind?open_id=…           look up token by open_id
  user opens it in a Databricks-logged-in         │ valid  ─▶ WorkspaceClient(token, auth_type="pat")
  browser → Apps OBO injects the user token       │         └▶ Genie Agent mode — runs AS the user
  GET /bind stores {open_id → token, exp}         │            (UC enforces THEIR grants) ─▶ Lark card
  → "绑定成功"                                      └ missing/expired ─▶ reply the bind link
```

One **single-instance** FastAPI app runs both halves in one process: a daemon thread
holds the Lark WebSocket long-connection (outbound — no public inbound webhook), and
FastAPI serves the browser-facing bind routes. The in-memory token map is why it must
stay single-instance.

### Agent mode (the default) vs chat mode

Questions go to Genie **Agent mode** — the same multi-step analysis the Genie UI runs:
it plans, runs as many SQL queries as it needs, makes charts, and writes a report. The
bot streams it (`POST /api/2.0/genie/agents/{space_id}/responses`) and shows a status
card that updates as each query runs (「已执行 N 个查询…」). The answer card has the
report (with tables and Genie's own charts as images), the SQL it ran, and a 「在Genie中查看」 button that opens that exact
conversation in Genie. A long report is split across several cards (（1/N）), never cut off.

**Chat mode** (the Genie Conversation API) answers with one query per turn, so on broad
questions it often asks for clarification instead. The bot uses it when
`GENIE_MODE=chat`, or automatically when Agent mode isn't enabled in the workspace
(it's in Beta, so the API answers 404 there).

Follow-ups stay in the same Genie conversation, so Genie keeps the context, like one
thread in the Genie UI. DM **`新对话`** to start fresh; after 30 idle minutes a new
question starts a new conversation anyway.

## Why per-user OBO

The simplest way to run a Lark → Genie bot is under **one shared identity** (e.g. a
continuous job or service running as a service principal) — but then *every* Lark
user queries Genie with the **same** permissions. This app keeps the same Lark
WebSocket transport and Genie APIs, and runs each question as the
**asking user** instead.

```
Shared identity (e.g. a job)                   This app — per-user OBO
────────────────────────────                   ───────────────────────
A ─┐                                            A ─▶ App ─▶ client(token=A) ─▶ Genie as A ─▶ A's data
B ─┼─▶ bot ─▶ WorkspaceClient()  ← one SP       B ─▶ App ─▶ client(token=B) ─▶ Genie as B ─▶ B's data
C ─┘        └▶ Genie as the SP ─▶ same data     (each user binds once; token cached ~1h)
```

| | Shared identity (e.g. a continuous job) | This app (per-user OBO) |
|---|---|---|
| Runs Genie as | one shared service principal | the asking user, per request |
| Data each user sees | whatever the SP can read — same for all | their own UC grants (row/column filters apply) |
| Query history / audit | attributed to the SP | attributed to the real user |
| Genie cost / quota | SP billed, no free allowance | each named user's free allowance* |
| Compute | continuous cluster, 24/7 | single-instance App container |
| Onboarding | none (just DM) | one-time bind per user; re-bind after ~1h / restart |
| State | stateless | in-memory token map → must stay single-instance |

\*Genie paygo terms change — check current pricing before quoting.

**Implications**
- **Governance (the point):** with a shared identity, every user has the SP's blast radius, so UC row/column masking and per-user grants are meaningless — everyone *is* the SP. OBO makes Unity Catalog enforce each person's real permissions, so the bot can safely sit over sensitive/segmented data.
- **Audit & trust:** query history names the real user, not an anonymous SP; the generated SQL is attributable (and shown on the card).
- **Cost:** per-user identity uses each named user's Genie allowance instead of SP billing.
- **The price:** more moving parts — a one-time bind (and re-bind after ~1h / app restart, since tokens live only in memory), a hard single-instance ceiling, compute billing while the app runs, and the OBO token lifecycle to manage.
- **Beyond a demo:** a custom OAuth app with `offline_access` (bind *once*) + a persistent encrypted token store (survives restarts, unlocks >1 instance) — see the spec's "Approach B".

## Set up the Lark bot

This is a one-time step in the Lark developer console, by someone who can manage apps in
your Lark tenant: [open.larksuite.com/app](https://open.larksuite.com/app) for Lark, or
[open.feishu.cn/app](https://open.feishu.cn/app) for Feishu (then set `LARK_REGION: cn`).
Menu names are given in English and Chinese.

1. **Create a custom app** (企业自建应用). Its name and icon are what people see in Lark,
   e.g. "Genie 数据分析助手".
2. **Add a bot:** Add Features (添加应用能力) → **Bot** (机器人).
3. **Add the scopes:** Permissions & Scopes (权限管理) → **Add permission scopes to app**.
   Each one must have type **Tenant token** (应用身份): the bot acts as the app, so
   user-token scopes don't work.

   | Scope (as the console names it) | Needed? | Used for |
   |---|---|---|
   | Get direct messages sent to bot — `im:message.p2p_msg:readonly` | Yes | Receiving the questions people DM the bot |
   | Read and send direct messages and group chat messages — `im:message` | Yes | Replying to messages |
   | Send messages as an app — `im:message:send_as_bot` | Yes | Sending answers and updating the status card |
   | Read and upload images or other files — `im:resource` | Optional | Uploading Genie's charts as images; without it the bot draws native Lark charts |
   | Receive users' mentions — `im:message.group_at_msg:readonly` | Only for group chats | Answering when someone @mentions the bot in a group. Its bind links are then visible to the whole group, so DMs are safer |

   Leave out the other group scopes:
   - The ones for *all* group messages: `im:message.group_msg` (sensitive),
     `im:message.group_msg:readonly` and `im:message.group_msg.include_bot:read`. With
     these, the bot receives every message in its groups and would answer each one.
   - Mentions from other bots: `im:message.group_at_msg.include_bot:readonly`.

4. **Copy the credentials:** Credentials & Basic Info (凭证与基础信息) → **App ID** and
   **App Secret**. They go into a Databricks secret scope when you deploy.
5. **Receive messages over a long connection:** Events & Callbacks (事件与回调) → Event
   configuration.
   - Subscription mode: **Receive events through persistent connection** (使用长连接接收事件).
     There's no URL to fill in: the app connects out to Lark. If Lark won't save the setting
     because no connection is detected, deploy the app first (deploy steps 1–3, either
     path); it connects as soon as it starts.
   - **Add event:** **Receive messages** (接收消息, `im.message.receive_v1`). The DM scope
     from step 3 is what lets this event reach the bot.
6. **Publish:** Version Management & Release (版本管理与发布) → create a version, set
   **Availability** (可用范围) to the people or departments who'll use the bot, and submit
   it. If your tenant requires approval, a Lark admin approves it in the Admin Console
   (管理后台). The bot is live once the version shows **Released** (已发布).
   - Changes to scopes or events take effect only in a new released version, and a newly
     added permission can take a while to reach the bot (~40 min in testing).

## Deploy with the Databricks CLI

### Prerequisites

- **Databricks CLI** (≥ 1.0) authenticated to the target workspace, e.g.
  `databricks auth login --host <workspace-url> --profile <profile>`.
- A **Genie space** (now called a *Genie Agent*) to answer from. You need **CAN MANAGE**
  on it: the deploy binds it to the app as a resource.
- A **Lark/Feishu bot**, set up as in [Set up the Lark bot](#set-up-the-lark-bot).
- Every Lark user who'll ask questions needs a **Databricks login** in the workspace
  (OBO runs as them).
- **One deployment per Lark bot.** A Lark bot allows one WebSocket connection; if two
  deployments share a bot, messages are split between them. Use a separate Lark app
  per environment, or stop the other deployment.

### 1. Store the Lark credentials in a secret scope

```bash
databricks secrets create-scope lark_bot --profile <profile>
databricks secrets put-secret lark_bot lark_app_id --profile <profile>       # prompts for the value
databricks secrets put-secret lark_bot lark_app_secret --profile <profile>
```

### 2. Point the bundle at your Genie space

The one required value is the Genie space ID — the `<ID>` in the space's URL
`/genie/rooms/<ID>`. Keep it out of git in the bundle's git-ignored override file:

```bash
mkdir -p .databricks/bundle/dev
echo '{"genie_space_id": "<ID>"}' > .databricks/bundle/dev/variable-overrides.json
```

…or pass `--var genie_space_id=<ID>` to every bundle command, or
`export BUNDLE_VAR_genie_space_id=<ID>`. The other variables have defaults (see
[Configuration](#configuration)).

### 3. Deploy and start

```bash
databricks bundle validate -t dev --profile <profile>
databricks bundle deploy   -t dev --profile <profile>
databricks bundle run lark_genie_bot -t dev --profile <profile>
```

`bundle run` prints the app URL. **Open it once in your browser** — it confirms the app
is up, and the bot learns its public URL from that visit if it can't read it from the
Apps API (see [How the app finds its URL](#how-the-app-finds-its-url)).

### 4. Give people access

- **The app:** **CAN USE** for the people (or a group) who'll use the bot — they open
  the bind page through it (app page → **Permissions**).
- **The data:** **CAN RUN** on the Genie space, `SELECT` on its tables, and **CAN USE**
  on its SQL warehouse. Genie runs as each user, so this is what gates their data.

### 5. Use it

In Lark, search for the bot by name and DM it **`绑定`** → open the link in a browser
that's logged in to the workspace → you'll see **绑定成功**. Then ask a data question; the answer runs under
your identity. Re-bind after ~1 h or an app restart.

A status card shows progress while Genie works (Agent mode takes ~30 s to a few
minutes), then the answer arrives as one or more cards: Genie's report with its tables and charts, the SQL
it ran (collapsible), and a 「在Genie中查看」 button that opens the conversation in Genie
with its charts. Ask follow-ups directly; DM **`新对话`** to change topic.

## Deploy without the CLI

No CLI access to the workspace? Clone the repo into a **Git folder** and follow
[`docs/deploy-to-new-workspace.md`](docs/deploy-to-new-workspace.md) — a setup notebook
creates the secret scope and, optionally, the app.

## Configuration

**Bundle variables** (`databricks.yml`):

| Variable | Default | Notes |
|---|---|---|
| `genie_space_id` | — (required) | The Genie space to answer from |
| `genie_space_name` | `Genie space` | Label for the Genie space resource on the app |
| `app_name` | `lark-genie-bot` | Lowercase letters, numbers, hyphens; unique per workspace |
| `secret_scope` | `lark_bot` | Holds the keys `lark_app_id` and `lark_app_secret` |

**App environment** (`src/app.yaml` — the same file works in every workspace):

| Variable | Source | Notes |
|---|---|---|
| `GENIE_SPACE_ID` | `valueFrom: genie-space` | The app's Genie space resource |
| `LARK_APP_ID` / `LARK_APP_SECRET` | `valueFrom` secret resources | From the secret scope |
| `LARK_REGION` | `intl` | `intl` = larksuite.com; set `cn` for Feishu (feishu.cn) |
| `ENV` | `prod` | Disables the local `DEV_USER_TOKEN` fallback |
| `BOT_TITLE` | optional | Card header; default `Genie 数据分析助手` |
| `GENIE_MODE` | optional | `agent` (default) or `chat` |
| `GENIE_AGENT_TIMEOUT_S` | optional | How long to wait for an Agent-mode answer before pointing to Genie; default `600` |
| `CONVERSATION_IDLE_S` | optional | Idle time after which a question starts a new Genie conversation; default `1800` |
| `APP_BASE_URL` | optional | Override for the app's public URL (normally discovered) |
| `DATABRICKS_HOST`, `DATABRICKS_APP_NAME`, `DATABRICKS_WORKSPACE_ID` | injected | Provided by the Apps runtime |
| `DISABLE_WS` / `DEV_USER_TOKEN` | local dev only | Skip the WS thread / inject a token at `/bind` locally |

OBO scopes are declared on the **app resource** (`resources/lark_bot.app.yml`), not in
`app.yaml`: `user_api_scopes: [genie, sql]` with `forward_user_access_token: true`.
(`genie` covers Agent mode; the older `dashboards.genie` is deprecated and maps to it.)

### How the app finds its URL

Bind links need the app's public URL, which Databricks Apps doesn't pass to the app.
`src/app/baseurl.py` resolves it at runtime, in order: `APP_BASE_URL` if set → the Apps
API for `DATABRICKS_APP_NAME` (called as the app's service principal) → the host of the
most recent browser request. The startup log line `bind links: …` shows what it found.
Until it knows the URL, the bot replies that it isn't ready rather than sending a
broken link.

## Architecture & layout

| File | Responsibility |
|------|----------------|
| `src/app/tokens.py` | Thread-safe in-memory store `open_id → {access_token, expires_at, email}`; `valid()` expires 60 s early so a token never dies mid-query |
| `src/app/genie_agent.py` | Agent mode: `run_agent()` streams `/genie/agents/{id}/responses` as the user (progress callback per SQL step); `download_chart()` fetches each chart Genie drew as a PNG, and `native_chart()` redraws it as a Lark chart from its query result when the image can't be uploaded; `build_cards()` renders the report — headings, native tables, charts, the SQL list, and a link to the conversation — over as many cards as Lark's size limit needs |
| `src/app/genie.py`  | Chat mode: `ask_genie_chat(client, space_id, question, conversation_id)` → `(card, conversation_id)` via the Conversation API, plus the shared Card 2.0 pieces (header, button, `strip_rich` fallback) and Genie-style number formatting |
| `src/app/conversations.py` | In-memory `open_id → current Genie conversation` (per mode, 30-min idle expiry) so follow-ups keep context |
| `src/app/bot.py`    | `route()` (bind / 新对话 / unbound / ask / auth-expiry branches; one question at a time per user; Agent mode with chat fallback), `bind_url()`, `_client_kwargs()` (pins `auth_type="pat"`), `_chart_images()` (uploads Genie's chart PNGs to Lark), the Lark adapter (status card edited in place; if Lark rejects a rich card it resends a plain copy), and a **supervised** WS reconnect loop |
| `src/app/baseurl.py` | Resolves the app's public URL for bind links (env override → Apps API → request host) |
| `src/app/app.py`    | FastAPI `GET /` (landing), `GET /bind` (captures the forwarded token), `GET /healthz`; startup launches the WS thread (skipped when `DISABLE_WS` is set) |

```
genie-lark-demo/
├── databricks.yml                 # DABs bundle: variables + a `dev` target (no workspace pinned)
├── resources/
│   └── lark_bot.app.yml           # the Databricks App resource (OBO scopes, Genie space + secrets, 1 instance)
├── src/                           # ← deploy root (app.yaml + requirements + the app/ package)
│   ├── app.yaml                   # uvicorn command + env (valueFrom resources; same in every workspace)
│   ├── requirements.txt
│   └── app/
│       ├── app.py  baseurl.py  bot.py  conversations.py  genie.py  genie_agent.py  tokens.py
├── deploy/
│   └── setup_target_workspace.py  # notebook for no-CLI deploys: secret scope + (optional) create/deploy the app
├── tests/                         # pytest unit tests, incl. guards against workspace-specific config
├── docs/
│   ├── deploy-to-new-workspace.md # no-CLI (UI) deploy runbook
│   └── superpowers/               # original design spec + implementation plan
└── pytest.ini
```

## Local development

```bash
python3 -m venv .venv
./.venv/bin/pip install -r src/requirements.txt pytest httpx pyyaml
./.venv/bin/pytest -q
```

You can run the WS bot locally against a Genie space with a personal token
(`DATABRICKS_HOST` + a PAT + `GENIE_SPACE_ID` + the `LARK_*` env vars), but **OBO
cannot be tested locally** — `x-forwarded-access-token` only exists in a deployed App.
For local `/bind` exercises, set `DEV_USER_TOKEN` (ignored once `ENV=prod`) and
`APP_BASE_URL=http://localhost:8000`.

## Operations

```bash
databricks apps get  <app_name> --profile <profile>     # state (RUNNING) + url
databricks apps logs <app_name> --profile <profile>     # startup "bind links: …", WS connect, errors (OAuth profile)
databricks apps stop <app_name> --profile <profile>     # stop compute (it bills while running)
```

## Security notes

- **Per-user identity, not a shared SP:** each query runs as the bound user; UC
  governs the data; query history attributes to the user; named-user Genie quota
  applies (vs. SP billing).
- Tokens live **only in process memory**, are never logged or persisted, and the
  bind success page shows scopes/expiry but never the token.
- `auth_type="pat"` is pinned on the per-user client because a deployed App
  auto-injects the SP's OAuth creds (otherwise the SDK raises `oauth and pat`).
- The app's service principal gets only **CAN VIEW** on the Genie space (to bind it as
  a resource) and **READ** on the two Lark secrets; Genie queries never run as it.
- **Known limitation (bind-link bearer risk):** `/bind?open_id=` is a private
  capability sent over a Lark DM. A forwarded/crafted link opened by a logged-in
  victim would store the *victim's* token under the *attacker's* `open_id`.
  Acceptable for an internal demo; the hardening is a short-lived signed `state`
  nonce (see the spec). Other deferred items are tracked in the spec/plan.

## References

- No-CLI deploy runbook: `docs/deploy-to-new-workspace.md`
- Design spec: `docs/superpowers/specs/2026-10-07-lark-genie-obo-app-design.md`
- Implementation plan: `docs/superpowers/plans/2026-10-07-lark-genie-obo-app.md`
  (both written for the first deployment; the README reflects the current design)
