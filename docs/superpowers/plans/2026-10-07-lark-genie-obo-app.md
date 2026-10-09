# Lark → Genie OBO App — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate the continuous-job Lark→Genie bot to a single-instance Databricks App that answers each Lark user's questions under their own Databricks identity via Apps on-behalf-of (OBO) user authorization.

**Architecture:** FastAPI app (uvicorn) deployed on Databricks Apps. A daemon-thread Lark WebSocket client holds the long-connection; `GET /bind` captures the user's `x-forwarded-access-token` (injected by Apps OBO) keyed by Lark `open_id` into an in-memory store; per question the WS thread builds a per-user `WorkspaceClient` and calls Genie, so Unity Catalog enforces that user's grants.

**Tech Stack:** Python 3.11, FastAPI, uvicorn, lark-oapi 1.7.3, databricks-sdk, pytest. DABs for deploy. Target: `<workspace>`.

**Spec:** `docs/superpowers/specs/2026-10-07-lark-genie-obo-app-design.md`

## Global Constraints

- **Single app instance** — the in-memory token map must not split across replicas.
- **No PAT in the deployed app.** Per-user calls use `WorkspaceClient(host=..., token=<forwarded token>)`; any app-shared op uses the SP via `Config()`.
- **Never log, print, or persist tokens.** The `/bind` success page shows scopes + expiry only.
- Lark creds come from the existing `lark_bot` secret scope (`lark_app_id`, `lark_app_secret`).
- `GENIE_SPACE_ID` default `<genie-space-id>`; `LARK_REGION=intl`.
- OBO scopes: `dashboards.genie`, `sql`, `iam.current-user:read`, `iam.access-control:read`.
- Keep the WS keepalive worker-thread pattern (run the slow Genie call off the WS event loop, as in `src/lark_bot.py:238-254`).

## Review Focus

- **Expired / near-expiry token** (within 60s of `exp`) → treated as unbound, bind link returned, no Genie call. [Task 1 test]
- **401/403 from Genie mid-use** → drop the stored token + reply "re-bind"; non-auth errors keep the token. [Task 3 test]
- **`绑定` command while already bound** → still returns a fresh link. [Task 3 test]
- **`/bind` with no `x-forwarded-access-token`** (opened outside Databricks) → clear 400, no crash; `DEV_USER_TOKEN` honored only when `ENV!=prod`. [Task 4 test]
- **Malformed Lark message** (no text / non-JSON content) → prompt the user, no crash. [Task 3 test]

---

### Task 1: `tokens.py` — in-memory token store

**Files:** Create `src/app/tokens.py`; Test `tests/test_tokens.py`

**Interfaces:**
- Produces: `Record` dataclass `(access_token: str, expires_at: float, email: str | None)`; `put(open_id, access_token, expires_at, email=None) -> None`; `get(open_id) -> Record | None`; `valid(rec: Record) -> bool` (False once within `_MARGIN_S=60` of `expires_at`). Module-level dict + `threading.Lock`.

- [ ] **Step 1: Write failing test** — `tests/test_tokens.py`
```python
import time
from app import tokens

def test_put_get_roundtrip():
    tokens.put("ou_1", "tok", time.time() + 3600, "a@b.com")
    rec = tokens.get("ou_1")
    assert rec is not None and rec.access_token == "tok" and rec.email == "a@b.com"

def test_get_unknown_is_none():
    assert tokens.get("nope") is None

def test_valid_true_when_future():
    tokens.put("ou_2", "t", time.time() + 3600, None)
    assert tokens.valid(tokens.get("ou_2")) is True

def test_valid_false_within_margin():
    tokens.put("ou_3", "t", time.time() + 30, None)  # inside 60s margin
    assert tokens.valid(tokens.get("ou_3")) is False
```
- [ ] **Step 2: Run, verify FAIL** — `./.venv/bin/pytest tests/test_tokens.py -v` → ModuleNotFoundError.
- [ ] **Step 3: Implement** — `src/app/tokens.py`
```python
import threading, time
from dataclasses import dataclass

_MARGIN_S = 60
_lock = threading.Lock()
_store: dict[str, "Record"] = {}

@dataclass
class Record:
    access_token: str
    expires_at: float
    email: str | None = None

def put(open_id: str, access_token: str, expires_at: float, email: str | None = None) -> None:
    with _lock:
        _store[open_id] = Record(access_token, expires_at, email)

def get(open_id: str) -> "Record | None":
    with _lock:
        return _store.get(open_id)

def valid(rec: "Record | None") -> bool:
    return bool(rec) and rec.expires_at - _MARGIN_S > time.time()

def drop(open_id: str) -> None:
    with _lock:
        _store.pop(open_id, None)
```
- [ ] **Step 4: Run, verify PASS.**
- [ ] **Step 5: Commit** — `feat: add in-memory OBO token store`

---

### Task 2: `genie.py` — per-user Genie query + Lark card

**Files:** Create `src/app/genie.py`; Test `tests/test_genie.py`

**Interfaces:**
- Consumes: a client exposing `.api_client.do(method, path, body=None) -> dict` (a `WorkspaceClient`).
- Produces: `ask_genie(client, space_id: str, question: str) -> dict` (a Lark interactive-card dict).

**Implementation:** Port **verbatim** from `src/lark_bot.py`: the formatting helpers `_ISO, _MONEY_HINTS, _SQL_KW, _clean, _fmt_money, _fmt_val, _pretty_sql, _build_card` (lines 102-196) and the body of `ask_genie` (lines 68-99). Changes: (a) `ask_genie(client, space_id, question)` — `space_id` is now a parameter, not a module global; (b) replace the module-global `_w` with the passed `client`: inside, use `client.api_client.do(method, path, body=body)` (drop the old module-level `_dbx`/`_w`/`GENIE_SPACE_ID` env reads). Keep the Chinese status strings and `PREVIEW_ROWS=10`.

- [ ] **Step 1: Write failing test** — `tests/test_genie.py`
```python
from app import genie

class FakeAPI:
    def do(self, method, path, body=None):
        if path.endswith("/start-conversation"):
            return {"conversation_id": "c1", "message_id": "m1"}
        if "/attachments/" in path and path.endswith("/query-result"):
            return {"statement_response": {"manifest": {"schema": {"columns": [{"name": "pair"}, {"name": "vol"}]}},
                                           "result": {"data_array": [["BTC-USDT", "123"], ["ETH-USDT", "45"]]}}}
        # message poll
        return {"status": "COMPLETED", "attachments": [
            {"attachment_id": "a1", "query": {"description": "Top pairs", "query": "SELECT pair, vol FROM t ORDER BY vol DESC"}}]}

class FakeClient:
    api_client = FakeAPI()

def test_ask_genie_builds_card_with_text_and_sql():
    card = genie.ask_genie(FakeClient(), "space1", "top pairs?")
    assert card["header"]["title"]["content"]
    blob = str(card["elements"])
    assert "Top pairs" in blob and "SELECT" in blob and "BTC-USDT" in blob

def test_ask_genie_non_completed_returns_status_message():
    class Stuck(FakeAPI):
        def do(self, method, path, body=None):
            if path.endswith("/start-conversation"):
                return {"conversation_id": "c", "message_id": "m"}
            return {"status": "FAILED"}
    class C: api_client = Stuck()
    card = genie.ask_genie(C(), "s", "q")
    assert "FAILED" in str(card["elements"])
```
- [ ] **Step 2: Run, verify FAIL** (ModuleNotFound). For the poll loop, keep `GENIE_TIMEOUT_S` small-circuit: the FakeAPI returns `COMPLETED`/`FAILED` on the first poll so the `while time.time() < deadline` loop exits immediately — no real sleep needed.
- [ ] **Step 3: Implement** the port described above.
- [ ] **Step 4: Run, verify PASS.**
- [ ] **Step 5: Commit** — `feat: port Genie query/formatting to a per-user client`

---

### Task 3: `bot.py` — Lark WS handler + routing

**Files:** Create `src/app/bot.py`; Test `tests/test_bot_routing.py`

**Interfaces:**
- Consumes: `tokens.get/valid/drop`; `genie.ask_genie`; env `DATABRICKS_HOST`, `GENIE_SPACE_ID`, `APP_BASE_URL`, `LARK_APP_ID`, `LARK_APP_SECRET`, `LARK_REGION`.
- Produces: `bind_url(open_id) -> str`; `route(open_id, text, replies, client_factory=_default_client_factory) -> None` (testable core; `replies` has `.text(msg)` and `.card(card)`); `run()` (builds the Lark client + reconnect loop — not unit-tested, ported from `src/lark_bot.py:257-298`).

**Design of `route`:**
1. `text` stripped & `@_user_1` removed (as `src/lark_bot.py:245`). Empty → `replies.text(<prompt>)`.
2. `text` in `{"绑定","bind","登录","login"}` → `replies.text("请复制…: " + bind_url(open_id))`.
3. `rec = tokens.get(open_id)`; `not tokens.valid(rec)` → `replies.text("请先绑定…: " + bind_url(open_id))`.
4. else: `client = client_factory(rec.access_token)`; try `card = genie.ask_genie(client, GENIE_SPACE_ID, text)` → `replies.card(card)`. Except auth error (status 401/403, or `databricks.sdk.errors.PermissionDenied`/`Unauthenticated`) → `tokens.drop(open_id)` + `replies.text("登录已过期，请重新绑定: " + bind_url(open_id))`. Except other → `replies.text(f"查询出错了：{e}")`.

- [ ] **Step 1: Write failing test** — `tests/test_bot_routing.py`
```python
import os, pytest
os.environ.setdefault("APP_BASE_URL", "https://app.example.com")
os.environ.setdefault("GENIE_SPACE_ID", "space1")
from app import bot, tokens
import time

class Replies:
    def __init__(self): self.texts=[]; self.cards=[]
    def text(self, m): self.texts.append(m)
    def card(self, c): self.cards.append(c)

def test_unbound_returns_bind_link():
    tokens.drop("ou"); r=Replies()
    bot.route("ou", "最近交易量?", r)
    assert any("/bind?open_id=ou" in t for t in r.texts)

def test_bind_command_returns_link_even_if_bound():
    tokens.put("ou", "t", time.time()+3600, None); r=Replies()
    bot.route("ou", "绑定", r)
    assert any("/bind?open_id=ou" in t for t in r.texts) and not r.cards

def test_bound_calls_genie_and_cards(monkeypatch):
    tokens.put("ou", "t", time.time()+3600, None); r=Replies()
    monkeypatch.setattr(bot.genie, "ask_genie", lambda c,s,q: {"ok": True})
    bot.route("ou", "q", r, client_factory=lambda tok: object())
    assert r.cards == [{"ok": True}]

def test_auth_error_drops_token(monkeypatch):
    tokens.put("ou", "t", time.time()+3600, None); r=Replies()
    def boom(c,s,q): raise bot._AuthError("401")
    monkeypatch.setattr(bot.genie, "ask_genie", boom)
    bot.route("ou", "q", r, client_factory=lambda tok: object())
    assert tokens.get("ou") is None and any("重新绑定" in t for t in r.texts)

def test_empty_text_prompts():
    tokens.put("ou","t",time.time()+3600,None); r=Replies()
    bot.route("ou", "   ", r)
    assert r.texts and not r.cards
```
- [ ] **Step 2: Run, verify FAIL.** (Define a small `_AuthError` + an `_is_auth_error(exc)` helper that also recognizes real SDK errors by status code / class name so production catches them.)
- [ ] **Step 3: Implement** `bind_url`, `route`, `_default_client_factory` (`WorkspaceClient(host=os.environ["DATABRICKS_HOST"], token=tok)`), and `run()` (port WS wiring from `src/lark_bot.py:199-298`, with `on_message` extracting `open_id`+text and dispatching to `route` on a worker thread, replies via the Lark SDK create/reply calls).
- [ ] **Step 4: Run, verify PASS.**
- [ ] **Step 5: Commit** — `feat: add Lark routing with per-user binding`

---

### Task 4: `app.py` — FastAPI routes + startup

**Files:** Create `src/app/app.py`; Test `tests/test_app.py`

**Interfaces:**
- Consumes: `tokens.put`; `bot.bind_url`, `bot.run`.
- Produces: FastAPI `app`; `GET /` (landing HTML), `GET /bind`, `GET /healthz`. Startup launches `threading.Thread(target=bot.run, daemon=True)` unless `DISABLE_WS` is set.

**Design of `/bind`:** read `request.headers.get("x-forwarded-access-token")`; if absent and `os.getenv("ENV")!="prod"` use `os.getenv("DEV_USER_TOKEN")`; if still absent → `HTMLResponse(status_code=400, ...)` "open from within Databricks". Else parse the JWT `exp` best-effort (`_jwt_exp(token)` — base64-decode the middle segment; fall back to `time.time()+3300`), `tokens.put(open_id, token, exp, email)`, return success HTML listing the granted scopes (read from the JWT `scope` claim if present) + minutes to expiry. Never render the token itself.

- [ ] **Step 1: Write failing test** — `tests/test_app.py`
```python
import os
os.environ["DISABLE_WS"] = "1"; os.environ["ENV"] = "prod"
from fastapi.testclient import TestClient
from app.app import app
from app import tokens

c = TestClient(app)

def test_healthz():
    assert c.get("/healthz").status_code == 200

def test_bind_stores_token():
    tokens.drop("ouX")
    r = c.get("/bind?open_id=ouX", headers={"x-forwarded-access-token": "abc.def.ghi"})
    assert r.status_code == 200
    assert tokens.get("ouX") is not None

def test_bind_missing_token_is_400():
    r = c.get("/bind?open_id=ouY")  # no header, ENV=prod
    assert r.status_code == 400
    assert tokens.get("ouY") is None
```
- [ ] **Step 2: Run, verify FAIL.**
- [ ] **Step 3: Implement** `app.py` (routes + `_jwt_exp`, startup thread guarded by `DISABLE_WS`).
- [ ] **Step 4: Run, verify PASS** — full suite `./.venv/bin/pytest -v`.
- [ ] **Step 5: Commit** — `feat: add FastAPI bind/health routes + WS startup`

---

### Task 5: Packaging — `app.yaml` + `requirements.txt`

**Files:** Create `src/app/app.yaml`, `src/app/requirements.txt`

- [ ] **Step 1:** `src/app/requirements.txt`:
```
lark-oapi==1.7.3
databricks-sdk
fastapi
uvicorn
```
- [ ] **Step 2:** `src/app/app.yaml`:
```yaml
command: ["uvicorn", "app.app:app", "--host", "0.0.0.0", "--port", "8000"]
env:
  - name: GENIE_SPACE_ID
    value: "<genie-space-id>"
  - name: LARK_REGION
    value: "intl"
  - name: APP_BASE_URL
    value: "SET_AFTER_FIRST_DEPLOY"
  - name: LARK_APP_ID
    valueFrom: "lark-app-id"
  - name: LARK_APP_SECRET
    valueFrom: "lark-app-secret"
```
  (Confirm exact `valueFrom`/secret-resource syntax against `databricks apps manifest`; the `valueFrom` keys map to app `secret` resources bound to `lark_bot/lark_app_id` and `lark_bot/lark_app_secret`.)
- [ ] **Step 3:** Local import smoke: `./.venv/bin/python -c "import app.app"` from `src/`.
- [ ] **Step 4: Commit** — `chore: app.yaml + requirements`

---

### Task 6: DABs app resource + retire the job

**Files:** Modify `databricks.yml`; Create `resources/lark_bot.app.yml`; Remove `resources/lark_bot.job.yml`, `src/lark_bot.py`

- [ ] **Step 1:** Confirm app-resource schema + the `user_api_scopes` field: `databricks bundle schema | grep -i user_api_scopes` and `databricks apps manifest --profile <profile>`.
- [ ] **Step 2:** `resources/lark_bot.app.yml` — an `app` resource named `lark-genie-bot`, `source_code_path: ../src/app`, `user_api_scopes: [dashboards.genie, sql, iam.current-user:read, iam.access-control:read]`, and the two `lark_bot` secret resources. (Exact keys from Step 1.)
- [ ] **Step 3:** Remove `resources/lark_bot.job.yml` and `src/lark_bot.py`. Keep the `include: resources/*.yml` and the `genie_space_id` variable in `databricks.yml`.
- [ ] **Step 4:** `databricks bundle validate --strict -t dev --profile <profile>` → OK.
- [ ] **Step 5: Commit** — `feat: DABs app resource; retire continuous job`

---

### Task 7: Deploy + smoke (the OBO proof)

- [ ] **Step 1:** `databricks bundle deploy -t dev --profile <profile>` then `databricks bundle run lark_genie_bot -t dev --profile <profile>`.
- [ ] **Step 2:** `databricks apps get lark-genie-bot --profile <profile> -o json` → copy `url`; set `APP_BASE_URL` in `app.yaml` to it; redeploy.
- [ ] **Step 3:** Grant the app's service principal READ on the `lark_bot` secret scope; confirm the old continuous job is still PAUSED.
- [ ] **Step 4: Smoke:** `GET /healthz` 200 → open `<url>/bind?open_id=test` in a Databricks-logged-in browser → "bound ✓" with `genie` scope. In Lark: `绑定` → open link → ask a question → answer card. Confirm in Databricks **query history** the SQL ran under *your* identity.
- [ ] **Step 5: Demo check:** a second user with different UC grants binds + asks the same question → sees different/filtered data.
- [ ] **Step 6: Commit** any config tweaks (e.g. final `APP_BASE_URL`).

---

## Self-Review

**1. Spec coverage:** §3 architecture → Tasks 3/4; §4 components → Tasks 1-4 (tokens/genie/bot/app); §5 flows → Task 3 (`route`) + Task 4 (`/bind`); §6 config/secrets → Tasks 5/6; §7 error/expiry → Tasks 1 (expiry) + 3 (401/绑定/malformed); §8 testing → each task's tests + Task 7 smoke; §9 deploy → Tasks 6/7; §10 layout → Tasks 1-6. No uncovered spec section.

**2. Placeholder scan:** No "TBD/handle edge cases" left as behavior. `SET_AFTER_FIRST_DEPLOY` and the `valueFrom` key names are explicit deploy-time values with a stated confirmation step (Task 5/6 Step 1), not vague placeholders.

**3. Type consistency:** `Record`/`put`/`get`/`valid`/`drop` used identically in Tasks 1,3,4. `ask_genie(client, space_id, question)` signature identical in Tasks 2,3. `route(open_id, text, replies, client_factory=)` identical in Task 3 tests + impl. `bind_url(open_id)` identical across Tasks 3,4.

**4. Review Focus coverage:** expiry→Task 1 `test_valid_false_within_margin`; 401→Task 3 `test_auth_error_drops_token`; 绑定→Task 3 `test_bind_command_returns_link_even_if_bound`; missing header→Task 4 `test_bind_missing_token_is_400`; malformed/empty→Task 3 `test_empty_text_prompts`. All covered.
