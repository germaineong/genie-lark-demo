"""FastAPI entrypoint for the Lark → Genie OBO Databricks App.

Serves the browser-facing bind routes and, at startup, launches the Lark
WebSocket bot on a daemon thread. The ONLY place a user's Databricks token is
captured is `GET /bind`, from the `x-forwarded-access-token` header that Apps
OBO injects when the user opens the app in a browser authenticated to the
workspace. Tokens are stored in memory (see app.tokens) and never rendered.
"""
import base64
import html
import json
import os
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app import baseurl, bot, tokens

_DEFAULT_TTL_S = 3300  # ~55 min, used when the token carries no readable exp


def _jwt_claims(token: str) -> dict:
    """Best-effort decode of a JWT payload (middle segment). Returns {} on anything
    that isn't a readable JWT — opaque tokens are fine, we just fall back on TTL."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if not os.environ.get("DISABLE_WS"):
        threading.Thread(target=bot.run, daemon=True).start()
    yield


app = FastAPI(title="Lark → Genie OBO Bot", lifespan=lifespan)


@app.middleware("http")
async def _learn_public_url(request: Request, call_next):
    # Apps doesn't inject the app's own URL; the proxy's X-Forwarded-Host on any
    # request says where users reach us (a fallback source for /bind links).
    baseurl.learn_from_host(request.headers.get("x-forwarded-host"))
    return await call_next(request)


@app.get("/healthz")
def healthz():
    return JSONResponse({"status": "ok"})


@app.get("/", response_class=HTMLResponse)
def index():
    return (
        "<html><body style='font-family:sans-serif;max-width:40em;margin:3em auto'>"
        "<h1>Lark → Genie Bot</h1>"
        "<p>在 Lark 中给机器人发送 <b>绑定</b> 获取你的专属绑定链接，"
        "即可按你自己的 Databricks 身份查询 Genie。</p>"
        "</body></html>"
    )


@app.get("/bind", response_class=HTMLResponse)
def bind(request: Request, open_id: str):
    token = request.headers.get("x-forwarded-access-token")
    if not token and os.environ.get("ENV") != "prod":
        token = os.environ.get("DEV_USER_TOKEN")  # local-dev only
    if not token:
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;max-width:40em;margin:3em auto'>"
            "<h1>无法绑定</h1><p>请在<b>已登录 Databricks 的浏览器</b>中打开此链接"
            "（从机器人发来的整段链接复制）。</p></body></html>",
            status_code=400,
        )

    claims = _jwt_claims(token)
    try:
        exp = float(claims["exp"])
    except (KeyError, TypeError, ValueError):
        exp = time.time() + _DEFAULT_TTL_S
    scopes = claims.get("scope") or claims.get("scp") or ""
    granted = set(scopes.split()) if isinstance(scopes, str) else {str(s) for s in scopes}
    email = claims.get("email") or request.headers.get("x-forwarded-email")
    tokens.put(open_id, token, exp, email)
    print(f"[bind] {open_id}: scopes={' '.join(sorted(granted)) or '(unreadable)'}", flush=True)

    mins = max(0, int((exp - time.time()) / 60))
    # escape — scope/email come from an unverified JWT or a header
    scope_html = f"<p>Token scopes: <code>{html.escape(str(scopes))}</code></p>" if scopes else ""
    who = f"<p>用户：<b>{html.escape(email)}</b></p>" if email else ""
    # A browser session from before the app's scopes changed keeps its old token
    # (e.g. dashboards.genie only); Agent mode needs `genie`. Signing out of the app
    # forces a fresh consent with the current scopes.
    warn = ""
    if granted and not granted & {"genie", "genie:read", "all-apis"}:
        warn = ("<p style='color:#b45309'>⚠️ 此授权缺少 <code>genie</code> 权限，深度分析（Agent 模式）"
                "将无法使用。请先<a href='/.auth/sign_out'>退出本应用</a>并关闭它的其他标签页，"
                "再重新打开绑定链接。</p>")
    return HTMLResponse(
        "<html><body style='font-family:sans-serif;max-width:40em;margin:3em auto'>"
        f"<h1>绑定成功</h1>{who}<p>Token 已保存，约 {mins} 分钟内有效。</p>"
        f"{warn}{scope_html}<p>请关闭此窗口，回到 Lark 继续提问。</p></body></html>"
    )
