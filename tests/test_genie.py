from app import genie


class _API:
    def do(self, method, path, body=None):
        if path.endswith("/start-conversation"):
            return {"conversation_id": "c1", "message_id": "m1"}
        if "/attachments/" in path and path.endswith("/query-result"):
            return {"statement_response": {
                "manifest": {"schema": {"columns": [
                    {"name": "segment"}, {"name": "revenue"}, {"name": "pct_of_total_revenue"}]}},
                "result": {"data_array": [
                    ["Enterprise", "42049560.7", "37.36"],
                    ["Mid-Market", "37576351.37", "33.39"],
                    ["SMB", "32914041.75", "29.25"]]}}}
        return {"status": "COMPLETED", "attachments": [
            {"attachment_id": "a1", "query": {
                "description": "各客户细分市场占总收入比例",
                "query": "SELECT `c`.`segment` FROM `cat`.`sch`.`orders` `o`"}}]}


class _Client:
    api_client = _API()


def _els(card):
    return card["body"]["elements"]


def test_card_is_schema_2_0():
    card = genie.ask_genie(_Client(), "s", "q")
    assert card["schema"] == "2.0"
    assert card["header"]["title"]["content"]


def test_card_title_is_generic_by_default_and_configurable(monkeypatch):
    monkeypatch.delenv("BOT_TITLE", raising=False)
    assert genie._build_card("hi", [], [], "")["header"]["title"]["content"] == "Genie 数据分析助手"
    monkeypatch.setenv("BOT_TITLE", "Acme 数据助手")
    assert genie._build_card("hi", [], [], "")["header"]["title"]["content"] == "Acme 数据助手"


def test_card_has_narrative_tiles_and_chart():
    els = _els(genie.ask_genie(_Client(), "s", "q"))
    tags = [e.get("tag") for e in els]
    assert "markdown" in tags       # Genie narrative
    assert "column_set" in tags     # KPI tiles
    assert "chart" in tags          # donut


def test_card_sql_in_collapsible_no_backticks():
    els = _els(genie.ask_genie(_Client(), "s", "q"))
    panels = [e for e in els if e.get("tag") == "collapsible_panel"]
    assert panels, "SQL should live in a collapsible panel"
    blob = str(panels[0])
    assert "SELECT" in blob and "`" not in blob
    assert not any(e.get("tag") == "markdown" and "执行的 SQL" in str(e.get("content", "")) for e in els)


def test_card_has_genie_one_button(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", "https://ws.cloud.databricks.com")
    els = _els(genie.ask_genie(_Client(), "space123", "q"))
    btns = [e for e in els if e.get("tag") == "button"]
    assert btns, "expected a 'View in Genie One' button"
    blob = str(btns[0])
    assert "ws.cloud.databricks.com/genie/rooms/space123" in blob
    assert "在Genie中查看" in blob


def test_strip_rich_removes_chart_and_button(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", "https://ws.cloud.databricks.com")
    stripped = genie.strip_rich(genie.ask_genie(_Client(), "s", "q"))
    tags = [e.get("tag") for e in stripped["body"]["elements"]]
    assert "chart" not in tags and "button" not in tags


class _ChatAPI:
    """Records calls; Genie's text answer comes BEFORE the query attachment here."""

    def __init__(self):
        self.calls = []

    def do(self, method, path, body=None):
        self.calls.append((method, path))
        if path.endswith("/start-conversation"):
            return {"conversation_id": "c1", "message_id": "m1"}
        if method == "POST" and path.endswith("/messages"):
            return {"conversation_id": "c9", "message_id": "m2", "id": "m2"}
        if path.endswith("/query-result"):
            return {"statement_response": {
                "manifest": {"schema": {"columns": [{"name": "segment"}, {"name": "revenue"}]}},
                "result": {"data_array": [["Enterprise", "100"]]}}}
        return {"status": "COMPLETED", "attachments": [
            {"attachment_id": "a2", "text": {"content": "企业客户贡献了最多收入。"}},
            {"attachment_id": "a1", "query": {"description": "按细分市场汇总收入", "query": "SELECT 1"}},
            {"attachment_id": "a3", "suggested_questions": {"questions": ["按地区呢？", "按月份趋势？"]}}]}


class _ChatClient:
    def __init__(self):
        self.api_client = _ChatAPI()


def test_chat_prefers_genies_answer_over_the_query_description():
    card, conv = genie.ask_genie_chat(_ChatClient(), "s", "q")
    blob = str(card["body"]["elements"])
    assert "企业客户贡献了最多收入" in blob and "按细分市场汇总收入" not in blob and conv == "c1"


def test_chat_shows_suggested_follow_up_questions():
    card, _ = genie.ask_genie_chat(_ChatClient(), "s", "q")
    blob = str(card["body"]["elements"])
    assert "按地区呢？" in blob and "按月份趋势？" in blob


def test_chat_follow_up_posts_into_the_existing_conversation():
    client = _ChatClient()
    _, conv = genie.ask_genie_chat(client, "s", "q2", conversation_id="c9")
    assert ("POST", "/api/2.0/genie/spaces/s/conversations/c9/messages") in client.api_client.calls
    assert not any(p.endswith("/start-conversation") for _, p in client.api_client.calls)
    assert conv == "c9"


def test_strip_rich_turns_tables_into_text():
    card = {"schema": "2.0", "body": {"elements": [
        {"tag": "table", "columns": [{"name": "c0", "display_name": "priority"},
                                     {"name": "c1", "display_name": "days"}],
         "rows": [{"c0": "Critical", "c1": "1.9"}]},
        {"tag": "button"}]}}
    els = genie.strip_rich(card)["body"]["elements"]
    assert [e["tag"] for e in els] == ["markdown"]
    assert "Critical" in els[0]["content"] and "1.9" in els[0]["content"]


def test_non_completed_returns_card_with_status():
    class _Stuck(_API):
        def do(self, method, path, body=None):
            if path.endswith("/start-conversation"):
                return {"conversation_id": "c", "message_id": "m"}
            return {"status": "FAILED"}

    class _C:
        api_client = _Stuck()

    card = genie.ask_genie(_C(), "s", "q")
    assert card["schema"] == "2.0" and "FAILED" in str(_els(card))
