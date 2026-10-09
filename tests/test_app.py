import os

os.environ["DISABLE_WS"] = "1"
os.environ["ENV"] = "prod"

from fastapi.testclient import TestClient  # noqa: E402

from app import tokens  # noqa: E402
from app.app import app  # noqa: E402

c = TestClient(app)


def test_healthz():
    assert c.get("/healthz").status_code == 200


def test_bind_stores_token():
    tokens.drop("ouX")
    r = c.get("/bind?open_id=ouX", headers={"x-forwarded-access-token": "abc.def.ghi"})
    assert r.status_code == 200
    assert tokens.get("ouX") is not None


def test_bind_missing_token_is_400():
    r = c.get("/bind?open_id=ouY")  # no header, ENV=prod → no dev fallback
    assert r.status_code == 400
    assert tokens.get("ouY") is None


def test_bind_escapes_email_in_html():
    tokens.drop("ouE")
    r = c.get("/bind?open_id=ouE",
              headers={"x-forwarded-access-token": "a.b.c", "x-forwarded-email": "<script>x</script>"})
    assert r.status_code == 200
    assert "<script>x</script>" not in r.text and "&lt;script&gt;" in r.text


def test_any_request_teaches_the_public_base_url(monkeypatch):
    from app import baseurl
    monkeypatch.delenv("APP_BASE_URL", raising=False)
    baseurl._reset()
    c.get("/healthz", headers={"x-forwarded-host": "my-bot-1.example.com"})
    assert baseurl.base_url(lookup=lambda: None) == "https://my-bot-1.example.com"
    baseurl._reset()


def _jwt(claims):
    import base64
    import json
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"h.{payload}.s"


def test_bind_warns_when_the_token_lacks_the_genie_scope():
    r = c.get("/bind?open_id=ouS1", headers={"x-forwarded-access-token": _jwt({"scope": "dashboards.genie sql"})})
    assert r.status_code == 200 and tokens.get("ouS1") is not None  # chat mode still works
    assert ".auth/sign_out" in r.text


def test_bind_does_not_warn_when_genie_scope_is_present():
    r = c.get("/bind?open_id=ouS2", headers={"x-forwarded-access-token": _jwt({"scope": "genie sql"})})
    assert r.status_code == 200 and ".auth/sign_out" not in r.text


def test_bind_tolerates_non_numeric_exp():
    import base64
    import json
    payload = base64.urlsafe_b64encode(json.dumps({"exp": "notanumber"}).encode()).decode().rstrip("=")
    r = c.get("/bind?open_id=ouX2", headers={"x-forwarded-access-token": f"h.{payload}.s"})
    assert r.status_code == 200
