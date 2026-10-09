"""The app's public base URL — needed to build each user's /bind link.

Databricks Apps injects DATABRICKS_APP_NAME but not the app's own URL, so it is
resolved at runtime instead of being configured per workspace:

1. ``APP_BASE_URL`` env — explicit override for unusual setups;
2. the Apps API for ``DATABRICKS_APP_NAME``, called as the app's service principal;
3. the ``X-Forwarded-Host`` of the latest request the app served (any browser visit).
"""
import os
import threading
import time

_RETRY_S = 600  # after a failed API lookup, wait this long before asking again

_lock = threading.Lock()
_api_url = None
_api_retry_at = 0.0
_learned = None


def _reset() -> None:
    """Forget everything resolved so far (tests)."""
    global _api_url, _api_retry_at, _learned
    with _lock:
        _api_url, _api_retry_at, _learned = None, 0.0, None


def learn_from_host(host) -> None:
    """Remember the public host the Apps proxy forwarded a request for."""
    global _learned
    if not host:
        return
    url = "https://" + host.strip().rstrip("/")
    with _lock:
        changed, _learned = url != _learned, url
    if changed:
        print(f"[baseurl] learned public URL from a request: {url}", flush=True)


def _lookup_via_api():
    name = os.environ.get("DATABRICKS_APP_NAME")
    if not name:
        return None
    from databricks.sdk import WorkspaceClient
    return WorkspaceClient().apps.get(name=name).url


def base_url(lookup=_lookup_via_api):
    """Best known public base URL (no trailing slash), or None if not known yet."""
    override = os.environ.get("APP_BASE_URL", "").strip()
    if override:
        return override.rstrip("/")

    global _api_url, _api_retry_at
    with _lock:
        if _api_url:
            return _api_url
        due = time.time() >= _api_retry_at
        if due:
            _api_retry_at = time.time() + _RETRY_S
    if due:
        try:
            url = lookup()
        except Exception as e:  # noqa: BLE001 — e.g. the app's SP can't read its own app
            print(f"[baseurl] app URL lookup via the Apps API failed: {e}", flush=True)
            url = None
        if url:
            with _lock:
                _api_url = url.rstrip("/")
                return _api_url
    with _lock:
        return _learned
