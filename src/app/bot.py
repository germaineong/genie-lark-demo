"""Lark WebSocket bot: routes each incoming message either to a bind prompt or to
Genie, called under the asking user's bound OBO token.

Questions go to Genie **Agent mode** (`app.genie_agent`) — the multi-step analysis
the Genie UI runs — with a live progress card while it works. If a workspace hasn't
enabled Agent mode, or `GENIE_MODE=chat`, the bot uses chat mode (`app.genie`).
Either way follow-ups continue the user's Genie conversation (`app.conversations`).

Module import is side-effect-free and reads no Lark/Databricks env — those are read
only inside `_serve_once()` / the client factory — so the routing core (`route`) is
unit-testable without any credentials.
"""
import json
import os
import threading
import time
import traceback

from app import baseurl, conversations, genie, genie_agent, tokens

BIND_COMMANDS = {"绑定", "bind", "登录", "login"}
NEW_CHAT_COMMANDS = {"新对话", "重新开始", "new", "/new", "reset"}
_NOT_READY = ("机器人尚未就绪：还不知道本应用的访问地址。请管理员在浏览器中打开一次本应用，"
              "或在 app.yaml 中设置 APP_BASE_URL。")
_BUSY = "上一个问题还在分析中，请稍候…"
_PROGRESS_MIN_S = 2  # don't edit the progress card more often than this
_MAX_CHARTS = 8

_agent_disabled = False  # set once Agent mode answers 404 here; then chat mode only
_inflight: set = set()
_inflight_lock = threading.Lock()


def _begin(open_id: str) -> bool:
    """Claim the user's single in-flight question; False if one is already running."""
    with _inflight_lock:
        if open_id in _inflight:
            return False
        _inflight.add(open_id)
        return True


def _end(open_id: str) -> None:
    with _inflight_lock:
        _inflight.discard(open_id)


def _mode() -> str:
    if _agent_disabled or os.environ.get("GENIE_MODE", "agent").strip().lower() == "chat":
        return "chat"
    return "agent"


class _AuthError(Exception):
    """Token rejected by Databricks (401/403) — the user must re-bind."""


def _is_auth_error(exc: Exception) -> bool:
    if isinstance(exc, (_AuthError, genie_agent.AgentAuthError)):
        return True
    if type(exc).__name__ in ("PermissionDenied", "Unauthenticated"):  # databricks.sdk.errors
        return True
    return str(getattr(exc, "error_code", "")) in ("PERMISSION_DENIED", "UNAUTHENTICATED")


def bind_url(open_id: str):
    """This user's bind link, or None while the app's public URL is still unknown."""
    base = baseurl.base_url()
    return f"{base}/bind?open_id={open_id}" if base else None


def _with_bind_link(msg: str, open_id: str) -> str:
    link = bind_url(open_id)
    return f"{msg}\n{link}" if link else _NOT_READY


def _client_kwargs(token: str) -> dict:
    # Pin auth_type=pat: a deployed Databricks App auto-injects the app's SP OAuth
    # creds as DATABRICKS_CLIENT_ID/SECRET. Without this pin the SDK sees both oauth
    # and pat configured and raises "more than one authorization method configured",
    # breaking every per-user Genie call. Pinning pat selects the user's OBO token.
    return {"host": os.environ["DATABRICKS_HOST"], "token": token, "auth_type": "pat"}


def _default_client_factory(token: str):
    from databricks.sdk import WorkspaceClient
    return WorkspaceClient(**_client_kwargs(token))


def route(open_id: str, text: str, replies, client_factory=_default_client_factory) -> None:
    """Decide what to do with one message and push zero or more replies.

    `replies` has `.text(str)`, `.card(dict)` and `.progress(str, final=False)` (a
    status card edited in place). Pure of Lark I/O so it can be unit-tested; the WS
    adapter in `_serve_once()` supplies a real `replies`.
    """
    text = (text or "").replace("@_user_1", "").strip()
    if not text:
        replies.text("请输入一个问题，例如：最近一个月的成本分解是怎样的？")
        return
    command = text.lower()
    if command in BIND_COMMANDS:
        replies.text(_with_bind_link("请复制下面整段链接到浏览器打开（需已登录 Databricks）：", open_id))
        return
    if command in NEW_CHAT_COMMANDS:
        conversations.drop(open_id)
        replies.text("好的，已开始新的对话，请直接提问。")
        return

    rec = tokens.get(open_id)
    if not tokens.valid(rec):
        replies.text(_with_bind_link("请先绑定 Databricks（按用户身份调用 Genie）。请复制链接到浏览器打开：", open_id))
        return

    if not _begin(open_id):
        replies.text(_BUSY)
        return
    try:
        _answer(open_id, text, replies, client_factory(rec.access_token))
    except Exception as e:  # noqa: BLE001 — translate auth vs other for the user
        replies.progress("⚠️ 分析未能完成", final=True)
        if _is_auth_error(e):
            print(f"[bot] token rejected, asking to re-bind: {e}", flush=True)
            tokens.drop(open_id)
            replies.text(_with_bind_link("登录已过期，请重新绑定：", open_id))
        elif isinstance(e, genie_agent.AgentBusy):
            replies.text(_BUSY)
        elif isinstance(e, genie_agent.AgentRateLimited):
            replies.text("Genie 当前请求较多，请稍后再试。")
        else:
            print(f"[bot] genie error: {e}", flush=True)
            replies.text(f"查询出错了：{e}")
    finally:
        _end(open_id)


def _answer(open_id: str, text: str, replies, client) -> None:
    global _agent_disabled
    space_id = os.environ.get("GENIE_SPACE_ID", "")
    if _mode() == "agent":
        try:
            _answer_agent(open_id, text, replies, client, space_id)
            return
        except genie_agent.AgentUnavailable as e:
            _agent_disabled = True
            print(f"[bot] Genie Agent mode unavailable here ({e}); using chat mode from now on", flush=True)
            replies.progress("改用快速问答模式…", final=True)
        except genie_agent.AgentForbidden as e:
            # Not an expired login: keep the token and still answer, in chat mode.
            print(f"[bot] Genie Agent mode refused ({e}); answering in chat mode", flush=True)
            replies.progress("改用快速问答模式…", final=True)
            if e.missing_scope:
                replies.text(_scope_hint())
    _answer_chat(open_id, text, replies, client, space_id)


def _scope_hint() -> str:
    sign_out = f"{baseurl.base_url() or ''}/.auth/sign_out"
    return ("提示：你当前的 Databricks 授权缺少 genie 权限，深度分析（Agent 模式）暂时无法使用，已改用快速问答。"
            f"请在浏览器中打开 {sign_out} 退出本应用（并关闭它的其他标签页），然后发送「绑定」重新授权。")


def _answer_agent(open_id: str, text: str, replies, client, space_id: str) -> None:
    started = time.time()
    replies.progress("🔎 正在分析你的问题…")

    def on_progress(n: int, title: str) -> None:
        replies.progress(f"🔎 正在分析… 已执行 {n} 个查询" + (f"：{title}" if title else ""))

    previous = conversations.get(open_id, "agent")
    try:
        res = genie_agent.run_agent(client, space_id, text, conversation_id=previous, on_progress=on_progress)
    except genie_agent.AgentUnavailable:
        if not previous:
            raise
        conversations.drop(open_id)  # the remembered conversation is gone — start a new one
        res = genie_agent.run_agent(client, space_id, text, on_progress=on_progress)
    conversations.put(open_id, res.conversation_id, "agent")
    images = _chart_images(client, space_id, res, replies)
    for card in genie_agent.build_cards(res, space_id, images):
        replies.card(card)
    if res.status == "completed":
        done = f"✅ 分析完成：执行了 {len(res.queries)} 个查询，用时 {int(time.time() - started)} 秒"
    elif res.status == "timeout":
        done = "⏳ 分析耗时较长，请在 Genie 中查看完整结果"
    else:
        done = "⚠️ 分析未能完成"
    replies.progress(done, final=True)


def _chart_images(client, space_id: str, res, replies) -> dict:
    """Genie's own rendered charts, uploaded to Lark: {attachment_id: image_key}.
    Charts that can't be fetched or uploaded stay as a text note in the card."""
    charts = genie_agent.charts(res)[:_MAX_CHARTS]
    if not charts or not res.message_id:
        return {}
    replies.progress(f"🖼️ 正在准备 {len(charts)} 个图表…")
    images = {}
    for attachment_id, _title in charts:
        png = genie_agent.download_chart(client, space_id, res.conversation_id, res.message_id, attachment_id)
        if not png:
            continue
        key = replies.image(png)
        if not key:
            break  # uploads are refused (e.g. no im:resource on the Lark app) — native charts fill in
        images[attachment_id] = key
    return images


def _answer_chat(open_id: str, text: str, replies, client, space_id: str) -> None:
    replies.text("正在查询，请稍候…")
    previous = conversations.get(open_id, "chat")
    try:
        card, conversation_id = genie.ask_genie_chat(client, space_id, text, conversation_id=previous)
    except Exception as e:  # noqa: BLE001 — a stale conversation shouldn't block a fresh ask
        if not previous or _is_auth_error(e):
            raise
        conversations.drop(open_id)
        card, conversation_id = genie.ask_genie_chat(client, space_id, text)
    conversations.put(open_id, conversation_id, "chat")
    replies.card(card)


def _supervise(serve_once, *, sleep_s: float = 5, _max_iters=None) -> None:
    """Call `serve_once()` forever; log loudly and retry on ANY exception so a
    setup/credential error (e.g. a missing Lark secret) retries instead of silently
    killing the WS thread. `_max_iters` bounds the loop for tests."""
    i = 0
    while _max_iters is None or i < _max_iters:
        try:
            serve_once()
        except Exception:
            print("[bot] WS attempt failed; retrying in "
                  f"{sleep_s}s:\n" + traceback.format_exc(), flush=True)
        time.sleep(sleep_s)
        i += 1


def _serve_once() -> None:
    """One WS connection attempt: read creds, build the Lark client, block on
    ws.start(). All env reads happen HERE (under _supervise) so a bad secret retries
    rather than dying once. Returns when the connection drops; _supervise retries."""
    import asyncio

    import lark_oapi as lark
    import lark_oapi.ws.client as _wsclient
    import io

    from lark_oapi.api.im.v1 import (
        CreateImageRequest,
        CreateImageRequestBody,
        P2ImMessageReceiveV1,
        PatchMessageRequest,
        PatchMessageRequestBody,
        ReplyMessageRequest,
        ReplyMessageRequestBody,
    )

    app_id = os.environ["LARK_APP_ID"]
    app_secret = os.environ["LARK_APP_SECRET"]
    domain = lark.LARK_DOMAIN if os.environ.get("LARK_REGION", "intl") == "intl" else lark.FEISHU_DOMAIN
    reply_client = lark.Client.builder().app_id(app_id).app_secret(app_secret).domain(domain).build()

    class _LarkReplies:
        def __init__(self, message_id: str):
            self._mid = message_id
            self._progress_mid = None  # the status card's message id; "" if it couldn't be sent
            self._progress_at = 0.0

        def image(self, png: bytes):
            """Upload a chart PNG to Lark; its image_key, or None if the upload fails
            (e.g. the Lark app lacks the image-upload permission)."""
            buf = io.BytesIO(png)
            buf.name = "chart.png"
            resp = reply_client.im.v1.image.create(
                CreateImageRequest.builder().request_body(
                    CreateImageRequestBody.builder().image_type("message").image(buf).build()
                ).build()
            )
            if getattr(resp, "success", lambda: False)() and getattr(resp, "data", None):
                return resp.data.image_key
            print(f"[bot] chart upload to Lark failed (code={getattr(resp, 'code', None)} "
                  f"msg={getattr(resp, 'msg', None)})", flush=True)
            return None

        def progress(self, text: str, final: bool = False) -> None:
            """One status card per question, edited in place while Genie works."""
            card = {"schema": "2.0", "config": {"update_multi": True},
                    "body": {"elements": [{"tag": "markdown", "content": text}]}}
            if self._progress_mid is None:
                if final:
                    return  # no status card was shown, so there's nothing to finish
                resp = self._reply("interactive", card)
                ok = getattr(resp, "success", lambda: False)() and getattr(resp, "data", None)
                self._progress_mid = (resp.data.message_id if ok else "") or ""
                self._progress_at = time.time()
                return
            if not self._progress_mid or (not final and time.time() - self._progress_at < _PROGRESS_MIN_S):
                return
            self._progress_at = time.time()
            resp = reply_client.im.v1.message.patch(
                PatchMessageRequest.builder().message_id(self._progress_mid).request_body(
                    PatchMessageRequestBody.builder().content(json.dumps(card)).build()
                ).build()
            )
            if not getattr(resp, "success", lambda: True)():
                print(f"[bot] progress update failed (code={getattr(resp, 'code', None)} "
                      f"msg={getattr(resp, 'msg', None)})", flush=True)

        def _reply(self, msg_type: str, content: dict):
            return reply_client.im.v1.message.reply(
                ReplyMessageRequest.builder().message_id(self._mid).request_body(
                    ReplyMessageRequestBody.builder().msg_type(msg_type)
                    .content(json.dumps(content)).build()
                ).build()
            )

        def text(self, text: str) -> None:
            self._reply("text", {"text": text})

        def card(self, card: dict) -> None:
            resp = self._reply("interactive", card)
            # If Lark rejects the card (e.g. an unsupported chart spec), resend a
            # chart-stripped copy so the user still gets tiles + text + SQL.
            if getattr(resp, "success", lambda: True)():
                return
            print(f"[bot] card rejected (code={getattr(resp, 'code', None)} "
                  f"msg={getattr(resp, 'msg', None)}); resending without chart/button", flush=True)
            self._reply("interactive", genie.strip_rich(card))

    def on_message(data: P2ImMessageReceiveV1) -> None:
        # Return fast so the SDK keepalive ping loop isn't blocked by the slow Genie
        # call (a slow handler triggers a 1011 keepalive-timeout disconnect).
        msg = data.event.message
        message_id = msg.message_id
        open_id = data.event.sender.sender_id.open_id
        try:
            text = json.loads(msg.content).get("text", "")
        except (json.JSONDecodeError, AttributeError):
            text = ""
        print(f"[bot] received from {open_id}: {text!r}", flush=True)
        threading.Thread(target=route, args=(open_id, text, _LarkReplies(message_id)), daemon=True).start()

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )

    # The lark SDK binds a module-level loop at import time; give this thread a fresh
    # loop and repoint the SDK global so ws.start()'s run_until_complete runs on a
    # clean, owned loop.
    new_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(new_loop)
    _wsclient.loop = new_loop
    try:
        ws = lark.ws.Client(app_id, app_secret, event_handler=handler,
                            domain=domain, log_level=lark.LogLevel.INFO)
        ws.start()  # blocks until the connection drops
    finally:
        new_loop.close()  # don't leak the loop/fds across reconnects


def run() -> None:
    """Entry point (daemon thread at app startup): hold the Lark WS, supervised so
    it reconnects on drop and never dies silently on a setup error."""
    base = baseurl.base_url()
    print(f"Lark → Genie OBO bot starting… Genie mode: {_mode()}; bind links: "
          f"{base or 'unknown until the app is opened in a browser (or set APP_BASE_URL)'}", flush=True)
    _supervise(_serve_once)
