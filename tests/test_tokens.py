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
