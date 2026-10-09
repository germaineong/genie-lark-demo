import pytest

from app import baseurl


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("APP_BASE_URL", raising=False)
    baseurl._reset()
    yield
    baseurl._reset()


def test_env_override_wins(monkeypatch):
    monkeypatch.setenv("APP_BASE_URL", "https://override.example.com/")
    baseurl.learn_from_host("learned.example.com")
    assert baseurl.base_url(lookup=lambda: "https://api.example.com") == "https://override.example.com"


def test_api_lookup_is_used_and_cached():
    calls = []

    def lookup():
        calls.append(1)
        return "https://my-bot-123.example.com"

    assert baseurl.base_url(lookup=lookup) == "https://my-bot-123.example.com"
    assert baseurl.base_url(lookup=lookup) == "https://my-bot-123.example.com"
    assert len(calls) == 1


def test_falls_back_to_learned_host_when_lookup_fails():
    def denied():
        raise PermissionError("app SP has no access to its own app")

    baseurl.learn_from_host("my-bot-123.example.com")
    assert baseurl.base_url(lookup=denied) == "https://my-bot-123.example.com"


def test_failed_lookup_is_not_retried_on_every_call():
    calls = []

    def denied():
        calls.append(1)
        raise RuntimeError("denied")

    baseurl.base_url(lookup=denied)
    baseurl.base_url(lookup=denied)
    assert len(calls) == 1


def test_learning_a_new_host_is_logged_once(capsys):
    baseurl.learn_from_host("my-bot-123.example.com")
    baseurl.learn_from_host("my-bot-123.example.com")
    out = capsys.readouterr().out
    assert out.count("https://my-bot-123.example.com") == 1


def test_none_when_nothing_known():
    assert baseurl.base_url(lookup=lambda: None) is None
