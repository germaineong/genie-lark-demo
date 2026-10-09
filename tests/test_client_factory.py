from app import bot


def test_client_kwargs_pins_pat_auth(monkeypatch):
    """The per-user client must pin auth_type=pat so the app's auto-injected SP
    OAuth creds (DATABRICKS_CLIENT_ID/SECRET) don't collide with the user PAT and
    raise 'more than one authorization method configured'. Kwargs are asserted
    directly to keep the test free of real client construction / network."""
    monkeypatch.setenv("DATABRICKS_HOST", "https://x.cloud.databricks.com")

    kw = bot._client_kwargs("user-obo-token")

    assert kw["token"] == "user-obo-token"
    assert kw["auth_type"] == "pat"
    assert kw["host"].startswith("https://")
