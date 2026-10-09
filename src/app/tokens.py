"""In-memory, per-user OBO token store, keyed by Lark open_id.

Single-instance only: this map lives in process memory and is intentionally not
persisted (Approach A). The app must run as one instance so a bind on one replica
is visible to the WS thread serving the next question. Tokens are never logged.
"""
import threading
import time
from dataclasses import dataclass

# Treat a token as expired this many seconds before its real expiry, so we never
# hand a token to Genie that dies mid-request.
_MARGIN_S = 60

_lock = threading.Lock()
_store: dict[str, "Record"] = {}


@dataclass
class Record:
    access_token: str
    expires_at: float  # epoch seconds
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
