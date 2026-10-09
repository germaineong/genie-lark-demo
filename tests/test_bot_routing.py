import os
import time

import pytest

os.environ.setdefault("APP_BASE_URL", "https://app.example.com")
os.environ.setdefault("GENIE_SPACE_ID", "space1")

from app import bot, conversations, genie_agent, tokens  # noqa: E402


class Replies:
    def __init__(self):
        self.texts = []
        self.cards = []
        self.progress_updates = []

    def text(self, m):
        self.texts.append(m)

    def card(self, c):
        self.cards.append(c)

    def progress(self, m, final=False):
        self.progress_updates.append(m)

    def image(self, png):
        self.images = getattr(self, "images", []) + [png]
        return f"img_{len(self.images)}"


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    monkeypatch.delenv("GENIE_MODE", raising=False)
    monkeypatch.setattr(bot, "_agent_disabled", False)
    conversations.drop("ou")
    yield
    conversations.drop("ou")


def _bound():
    tokens.put("ou", "t", time.time() + 3600, None)


def _fake_agent(calls, conversation_id="c1", raise_exc=None):
    def run_agent(client, space_id, question, conversation_id=None, on_progress=None, **kw):
        calls.append({"question": question, "conversation_id": conversation_id})
        if raise_exc:
            raise raise_exc
        if on_progress:
            on_progress(1, "收入占比")
        return genie_agent.AgentResult(conversation_id=_conv, status="completed")
    _conv = conversation_id
    return run_agent


def test_unbound_returns_bind_link():
    tokens.drop("ou")
    r = Replies()
    bot.route("ou", "最近交易量?", r)
    assert any("/bind?open_id=ou" in t for t in r.texts) and not r.cards


def test_bind_command_returns_link_even_if_bound():
    _bound()
    r = Replies()
    bot.route("ou", "绑定", r)
    assert any("/bind?open_id=ou" in t for t in r.texts) and not r.cards


def test_agent_mode_is_the_default_and_streams_progress_then_a_card(monkeypatch):
    _bound()
    calls = []
    monkeypatch.setattr(bot.genie_agent, "run_agent", _fake_agent(calls))
    monkeypatch.setattr(bot.genie_agent, "build_cards", lambda res, space_id, images=None: [{"agent": res.conversation_id}])
    r = Replies()
    bot.route("ou", "各细分市场收入占比？", r, client_factory=lambda tok: object())
    assert calls[0]["question"] == "各细分市场收入占比？"
    assert r.cards == [{"agent": "c1"}]
    assert any("收入占比" in p for p in r.progress_updates)


def test_follow_up_continues_the_same_genie_conversation(monkeypatch):
    _bound()
    calls = []
    monkeypatch.setattr(bot.genie_agent, "run_agent", _fake_agent(calls))
    monkeypatch.setattr(bot.genie_agent, "build_cards", lambda res, space_id, images=None: [{}])
    bot.route("ou", "q1", Replies(), client_factory=lambda tok: object())
    bot.route("ou", "q2", Replies(), client_factory=lambda tok: object())
    assert [c["conversation_id"] for c in calls] == [None, "c1"]


def test_new_chat_command_forgets_the_conversation(monkeypatch):
    _bound()
    calls = []
    monkeypatch.setattr(bot.genie_agent, "run_agent", _fake_agent(calls))
    monkeypatch.setattr(bot.genie_agent, "build_cards", lambda res, space_id, images=None: [{}])
    bot.route("ou", "q1", Replies(), client_factory=lambda tok: object())
    r = Replies()
    bot.route("ou", "新对话", r, client_factory=lambda tok: object())
    bot.route("ou", "q2", Replies(), client_factory=lambda tok: object())
    assert r.texts and not r.cards
    assert [c["conversation_id"] for c in calls] == [None, None]


def test_falls_back_to_chat_mode_when_agent_mode_is_unavailable(monkeypatch):
    _bound()
    agent_calls, chat_calls = [], []
    monkeypatch.setattr(bot.genie_agent, "run_agent",
                        _fake_agent(agent_calls, raise_exc=genie_agent.AgentUnavailable("404")))

    def chat(client, space_id, question, conversation_id=None):
        chat_calls.append(question)
        return {"chat": True}, "cc1"

    monkeypatch.setattr(bot.genie, "ask_genie_chat", chat)
    r = Replies()
    bot.route("ou", "q1", r, client_factory=lambda tok: object())
    bot.route("ou", "q2", Replies(), client_factory=lambda tok: object())
    assert r.cards == [{"chat": True}]
    assert len(agent_calls) == 1 and chat_calls == ["q1", "q2"]  # stays on chat once agent mode is off


def test_chat_mode_can_be_forced_and_keeps_context(monkeypatch):
    _bound()
    monkeypatch.setenv("GENIE_MODE", "chat")
    seen = []

    def chat(client, space_id, question, conversation_id=None):
        seen.append(conversation_id)
        return {"ok": True}, "cc1"

    monkeypatch.setattr(bot.genie, "ask_genie_chat", chat)
    r = Replies()
    bot.route("ou", "q1", r, client_factory=lambda tok: object())
    bot.route("ou", "q2", Replies(), client_factory=lambda tok: object())
    assert r.cards == [{"ok": True}] and seen == [None, "cc1"]


def test_agent_403_answers_in_chat_mode_keeps_the_token_and_explains(monkeypatch):
    _bound()
    agent_calls, chat_calls = [], []
    forbidden = genie_agent.AgentForbidden("HTTP 403: Invalid scope, required scopes: genie", missing_scope=True)
    monkeypatch.setattr(bot.genie_agent, "run_agent", _fake_agent(agent_calls, raise_exc=forbidden))

    def chat(client, space_id, question, conversation_id=None):
        chat_calls.append(question)
        return {"chat": True}, "cc1"

    monkeypatch.setattr(bot.genie, "ask_genie_chat", chat)
    r = Replies()
    bot.route("ou", "q1", r, client_factory=lambda tok: object())
    assert r.cards == [{"chat": True}] and tokens.get("ou") is not None
    assert any(".auth/sign_out" in t for t in r.texts)  # tells the user how to refresh consent
    bot.route("ou", "q2", Replies(), client_factory=lambda tok: object())
    assert len(agent_calls) == 2  # agent mode isn't switched off for everyone


def test_auth_error_drops_token(monkeypatch):
    _bound()
    monkeypatch.setattr(bot.genie_agent, "run_agent",
                        _fake_agent([], raise_exc=genie_agent.AgentAuthError("401")))
    r = Replies()
    bot.route("ou", "q", r, client_factory=lambda tok: object())
    assert tokens.get("ou") is None and any("重新绑定" in t for t in r.texts)


def test_a_second_question_while_one_is_running_is_told_to_wait(monkeypatch):
    _bound()
    calls = []
    monkeypatch.setattr(bot.genie_agent, "run_agent", _fake_agent(calls))
    assert bot._begin("ou")
    try:
        r = Replies()
        bot.route("ou", "q", r, client_factory=lambda tok: object())
    finally:
        bot._end("ou")
    assert not calls and r.texts and "稍候" in r.texts[0]


def test_agent_busy_conflict_asks_to_wait(monkeypatch):
    _bound()
    monkeypatch.setattr(bot.genie_agent, "run_agent",
                        _fake_agent([], raise_exc=genie_agent.AgentBusy("409")))
    r = Replies()
    bot.route("ou", "q", r, client_factory=lambda tok: object())
    assert any("稍候" in t for t in r.texts) and not r.cards


def test_empty_text_prompts():
    _bound()
    r = Replies()
    bot.route("ou", "   ", r)
    assert r.texts and not r.cards


class PermissionDenied(Exception):
    """Stands in for databricks.sdk.errors.PermissionDenied (matched by class name)."""


class Unauthenticated(Exception):
    """Stands in for databricks.sdk.errors.Unauthenticated."""


def test_is_auth_error_only_on_real_auth_errors():
    assert bot._is_auth_error(bot._AuthError("x")) is True
    assert bot._is_auth_error(genie_agent.AgentAuthError("401")) is True
    assert bot._is_auth_error(Unauthenticated("expired")) is True
    assert bot._is_auth_error(PermissionDenied("nope")) is False  # a 403 is missing access, not an expired login
    assert bot._is_auth_error(ValueError("row 403 of 500")) is False  # digit substring must not false-positive


def _chat_raising(exc, calls=None):
    def chat(client, space_id, question, conversation_id=None):
        if calls is not None:
            calls.append(conversation_id)
        raise exc
    return chat


def test_permission_denied_keeps_the_token_and_says_what_access_is_missing(monkeypatch):
    _bound()
    monkeypatch.setenv("GENIE_MODE", "chat")
    monkeypatch.setattr(bot.genie, "ask_genie_chat",
                        _chat_raising(PermissionDenied("User does not have CAN RUN on this space")))
    r = Replies()
    bot.route("ou", "q", r, client_factory=lambda tok: object())
    assert tokens.get("ou") is not None  # re-binding wouldn't help, so don't make them
    assert not any("/bind?open_id=" in t or "重新绑定" in t for t in r.texts)
    assert any("CAN RUN" in t and "SELECT" in t for t in r.texts)


def test_permission_denied_is_not_retried_in_a_new_conversation(monkeypatch):
    _bound()
    monkeypatch.setenv("GENIE_MODE", "chat")
    conversations.put("ou", "old-conv", "chat")
    calls = []
    monkeypatch.setattr(bot.genie, "ask_genie_chat", _chat_raising(PermissionDenied("no access"), calls))
    bot.route("ou", "q", Replies(), client_factory=lambda tok: object())
    assert calls == ["old-conv"]  # a fresh conversation would be refused the same way


def test_permission_denied_for_a_missing_scope_asks_to_sign_out_and_rebind(monkeypatch):
    _bound()
    monkeypatch.setenv("GENIE_MODE", "chat")
    monkeypatch.setattr(bot.genie, "ask_genie_chat",
                        _chat_raising(PermissionDenied("Invalid scope, required scopes: genie")))
    r = Replies()
    bot.route("ou", "q", r, client_factory=lambda tok: object())
    assert tokens.get("ou") is None  # that token can never work
    assert any(".auth/sign_out" in t and "绑定" in t for t in r.texts)


def test_unbound_without_a_known_app_url_explains_setup(monkeypatch):
    monkeypatch.setattr(bot.baseurl, "base_url", lambda: None)
    tokens.drop("ou")
    r = Replies()
    bot.route("ou", "最近交易量?", r)
    assert r.texts and "/bind" not in r.texts[0] and "APP_BASE_URL" in r.texts[0] and not r.cards


def test_bind_command_is_case_insensitive():
    _bound()
    r = Replies()
    bot.route("ou", "LOGIN", r, client_factory=lambda tok: object())
    assert any("/bind?open_id=ou" in t for t in r.texts) and not r.cards


def test_agent_charts_are_downloaded_and_uploaded_to_lark(monkeypatch):
    _bound()
    res = genie_agent.AgentResult(conversation_id="c1", status="completed", message_id="m1",
                                  parts=[{"viz": "v1"}], viz_titles={"v1": "收入占比图"})
    monkeypatch.setattr(bot.genie_agent, "run_agent", lambda *a, **k: res)
    monkeypatch.setattr(bot.genie_agent, "download_chart", lambda client, s, c, m, a: b"png-bytes")
    seen = {}

    def build_cards(result, space_id, images=None):
        seen["images"] = images
        return [{"card": 1}, {"card": 2}]

    monkeypatch.setattr(bot.genie_agent, "build_cards", build_cards)
    r = Replies()
    bot.route("ou", "q", r, client_factory=lambda tok: object())
    assert seen["images"] == {"v1": "img_1"} and r.images == [b"png-bytes"]
    assert r.cards == [{"card": 1}, {"card": 2}]  # every page is sent


def test_chart_uploads_stop_after_the_first_failure(monkeypatch):
    _bound()
    res = genie_agent.AgentResult(conversation_id="c1", status="completed", message_id="m1",
                                  parts=[{"viz": "v1"}, {"viz": "v2"}, {"viz": "v3"}])
    monkeypatch.setattr(bot.genie_agent, "run_agent", lambda *a, **k: res)
    monkeypatch.setattr(bot.genie_agent, "download_chart", lambda *a: b"png")
    monkeypatch.setattr(bot.genie_agent, "build_cards", lambda result, space_id, images=None: [{}])

    class NoUploads(Replies):
        def image(self, png):
            self.attempts = getattr(self, "attempts", 0) + 1
            return None

    r = NoUploads()
    bot.route("ou", "q", r, client_factory=lambda tok: object())
    assert r.attempts == 1  # e.g. a missing Lark permission — don't pay for it on every chart
