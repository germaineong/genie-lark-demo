from app import conversations


def test_follow_ups_reuse_the_conversation_for_the_same_mode():
    conversations.drop("ou")
    conversations.put("ou", "c1", "agent")
    assert conversations.get("ou", "agent") == "c1"
    assert conversations.get("ou", "chat") is None  # never mix chat and agent conversations


def test_idle_conversations_expire(monkeypatch):
    conversations.put("ou", "c1", "agent")
    monkeypatch.setattr(conversations, "_IDLE_S", -1)
    assert conversations.get("ou", "agent") is None


def test_drop_starts_fresh():
    conversations.put("ou", "c1", "agent")
    conversations.drop("ou")
    assert conversations.get("ou", "agent") is None
