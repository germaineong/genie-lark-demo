"""Which Genie conversation each Lark user is in, so follow-ups keep their context
(like one thread in the Genie UI). In memory, keyed by Lark `open_id` — the app is
single-instance. An idle conversation expires so a new topic starts fresh; `新对话`
resets explicitly. Chat- and agent-mode conversations are never mixed.
"""
import os
import threading
import time

_IDLE_S = int(os.environ.get("CONVERSATION_IDLE_S", "1800"))

_lock = threading.Lock()
_store: dict = {}  # open_id -> (conversation_id, mode, last_used)


def get(open_id: str, mode: str):
    """The user's current conversation for `mode`, or None to start a new one."""
    with _lock:
        rec = _store.get(open_id)
    if not rec:
        return None
    conversation_id, rec_mode, last_used = rec
    if rec_mode != mode or time.time() - last_used > _IDLE_S:
        return None
    return conversation_id


def put(open_id: str, conversation_id, mode: str) -> None:
    if conversation_id:
        with _lock:
            _store[open_id] = (conversation_id, mode, time.time())


def drop(open_id: str) -> None:
    with _lock:
        _store.pop(open_id, None)
