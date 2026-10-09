import json

import pytest

from app import genie_agent

HOST = "https://ws.example.com"


def _sse(*events):
    """Encode events the way the Agent API streams them: `event:` + `data:` lines (UTF-8)."""
    out = []
    for ev in events:
        out += [f"event:{ev['type']}".encode(), ("data:" + json.dumps(ev, ensure_ascii=False)).encode("utf-8"), b""]
    return out


CREATED = {"type": "response.created", "sequence_number": 0,
           "response": {"id": "r1", "status": "in_progress", "output": [], "conversation_id": "conv1"}}
SQL_CALL = {"type": "response.output_item.done", "output_index": 0, "item": {
    "type": "function_call", "id": "t1", "call_id": "t1", "status": "completed", "name": "execute_sql",
    "arguments": json.dumps({"title": "各细分市场收入占比",
                             "sql": "SELECT segment, SUM(revenue) FROM orders GROUP BY 1"}, ensure_ascii=False)}}
SQL_OUT = {"type": "response.output_item.done", "output_index": 1, "item": {
    "type": "function_call_output", "id": "t1_output", "call_id": "t1", "status": "completed",
    "output": "**各细分市场收入占比**\n\n| segment | revenue |\n| --- | --- |\n| Enterprise | 4.2049561E7 |"}}
VIZ_CALL = {"type": "response.output_item.done", "output_index": 2, "item": {
    "type": "function_call", "id": "v1", "call_id": "v1", "status": "completed", "name": "generate_visualization",
    "arguments": json.dumps({"title": "收入占比图", "query_attachment_id": "t1"}, ensure_ascii=False)}}
MSG = {"type": "response.output_item.done", "output_index": 3, "item": {
    "type": "message", "role": "assistant", "content": [
        {"type": "output_text", "text": "## 收入占比\n\n企业客户占比最高：**37.4%**"},
        {"type": "output_text", "text": "", "metadata": {"viz": {"attachment_id": "v1"}}},
        {"type": "output_text", "text": "总体均衡 \\[[1](https://ws.example.com/genie/rooms/s1/chats/conv1?o=1&gra_focus=t1)\\]"}]}}
COMPLETED = {"type": "response.completed",
             "response": {"id": "r1", "status": "completed", "conversation_id": "conv1", "output": []}}


class _Resp:
    def __init__(self, status=200, lines=(), text=""):
        self.status_code, self._lines, self.text, self.closed = status, list(lines), text, False

    def iter_lines(self):
        return iter(self._lines)

    def close(self):
        self.closed = True


class _Cfg:
    host = HOST

    def authenticate(self):
        return {"Authorization": "Bearer user-token"}


class _Client:
    config = _Cfg()


def _post_returning(resp, calls=None):
    def post(url, **kw):
        if calls is not None:
            calls.append((url, kw))
        return resp
    return post


def _full_stream():
    return _Resp(lines=_sse(CREATED, SQL_CALL, SQL_OUT, VIZ_CALL, MSG, COMPLETED))


def test_stream_is_parsed_into_a_result():
    res = genie_agent.run_agent(_Client(), "s1", "q", post=_post_returning(_full_stream()))
    assert res.status == "completed" and res.conversation_id == "conv1"
    assert [q.title for q in res.queries] == ["各细分市场收入占比"]
    assert res.queries[0].sql.startswith("SELECT")
    assert res.parts[0]["text"].startswith("## 收入占比") and res.parts[1] == {"viz": "v1"}
    assert res.viz_titles == {"v1": "收入占比图"}


def test_posts_the_question_as_the_user_and_continues_a_conversation():
    calls = []
    genie_agent.run_agent(_Client(), "s1", "下个月呢？", conversation_id="conv1",
                          post=_post_returning(_full_stream(), calls))
    url, kw = calls[0]
    assert url == f"{HOST}/api/2.0/genie/agents/s1/responses"
    assert kw["headers"]["Authorization"] == "Bearer user-token" and kw["stream"] is True
    assert kw["json"]["input"][0]["content"][0]["text"] == "下个月呢？"
    assert kw["json"]["conversation_id"] == "conv1"


def test_progress_is_reported_for_each_sql_step_only():
    seen = []
    genie_agent.run_agent(_Client(), "s1", "q", on_progress=lambda n, title: seen.append((n, title)),
                          post=_post_returning(_full_stream()))
    assert seen == [(1, "各细分市场收入占比")]  # the visualization call is not a query


def test_response_is_closed_after_reading():
    resp = _full_stream()
    genie_agent.run_agent(_Client(), "s1", "q", post=_post_returning(resp))
    assert resp.closed


@pytest.mark.parametrize("status,exc", [
    (404, genie_agent.AgentUnavailable),
    (401, genie_agent.AgentAuthError),
    (403, genie_agent.AgentForbidden),
    (409, genie_agent.AgentBusy),
    (429, genie_agent.AgentRateLimited),
])
def test_http_errors_map_to_specific_exceptions(status, exc):
    with pytest.raises(exc):
        genie_agent.run_agent(_Client(), "s1", "q", post=_post_returning(_Resp(status, text="nope")))


def test_403_for_a_missing_genie_scope_is_recognised():
    body = '{"error_code":"PERMISSION_DENIED","message":"Invalid scope, required scopes: genie"}'
    with pytest.raises(genie_agent.AgentForbidden) as e:
        genie_agent.run_agent(_Client(), "s1", "q", post=_post_returning(_Resp(403, text=body)))
    assert e.value.missing_scope is True


def test_failed_response_carries_the_error():
    failed = {"type": "response.failed", "response": {"status": "failed", "error": {"message": "boom"}}}
    res = genie_agent.run_agent(_Client(), "s1", "q", post=_post_returning(_Resp(lines=_sse(CREATED, failed))))
    assert res.status == "failed" and "boom" in res.error and res.conversation_id == "conv1"


def test_timeout_keeps_the_conversation_so_it_can_be_opened_in_genie():
    res = genie_agent.run_agent(_Client(), "s1", "q", timeout_s=-1, post=_post_returning(_full_stream()))
    assert res.status == "timeout" and res.conversation_id == "conv1"


def test_report_markdown_is_normalized_for_lark():
    md = genie_agent.lark_markdown("## 收入 **占比**\n内容 \\[[1](https://x.example.com/a?b=1)\\] 与 \\[[12](https://x.example.com/c)\\]")
    assert md == "**收入 占比**\n内容 [¹](https://x.example.com/a?b=1) 与 [¹²](https://x.example.com/c)"


def test_report_tables_become_table_components_with_glued_text_split_off():
    text = ("前言\n\n| priority | avg_resolution_days |\n| --- | --- |\n"
            "| Critical | 1.9 |\n| Low | 9.5 |**Key findings:**\n- 关键结论")
    els = genie_agent.report_elements([{"text": text}], {})
    assert [e["tag"] for e in els] == ["markdown", "table", "markdown"]
    table = els[1]
    assert [c["display_name"] for c in table["columns"]] == ["priority", "avg resolution days"]
    assert [list(r.values()) for r in table["rows"]] == [["Critical", "1.9"], ["Low", "9.5"]]
    assert els[2]["content"].startswith("**Key findings:**")


def test_scientific_notation_in_tables_is_made_readable():
    text = "| segment | revenue |\n| --- | --- |\n| Enterprise | 4.2049561E7 |"
    table = genie_agent.report_elements([{"text": text}], {})[0]
    assert list(table["rows"][0].values()) == ["Enterprise", "$4,205万"]


def test_viz_placeholder_names_the_chart():
    els = genie_agent.report_elements([{"viz": "v1"}], {"v1": "收入占比图"})
    assert "📊" in els[0]["content"] and "收入占比图" in els[0]["content"]


def test_card_has_report_sql_panel_and_link_to_the_conversation(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", HOST)
    monkeypatch.setenv("DATABRICKS_WORKSPACE_ID", "123")
    res = genie_agent.run_agent(_Client(), "s1", "q", post=_post_returning(_full_stream()))
    cards = genie_agent.build_cards(res, "s1")
    card, els = cards[0], cards[-1]["body"]["elements"]
    assert card["schema"] == "2.0" and card["header"]["title"]["content"]
    panel = next(e for e in els if e.get("tag") == "collapsible_panel")
    assert "1 条" in str(panel["header"]) and "SELECT" in str(panel["elements"])
    button = next(e for e in els if e.get("tag") == "button")
    assert button["behaviors"][0]["default_url"] == f"{HOST}/genie/rooms/s1/chats/conv1?o=123"


def test_timed_out_card_points_to_genie(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", HOST)
    res = genie_agent.AgentResult(conversation_id="conv1", status="timeout")
    els = genie_agent.build_cards(res, "s1")[-1]["body"]["elements"]
    assert "Genie" in els[0]["content"] and any(e.get("tag") == "button" for e in els)


def test_message_id_and_charts_are_collected():
    res = genie_agent.run_agent(_Client(), "s1", "q", post=_post_returning(_full_stream()))
    assert genie_agent.charts(res) == [("v1", "收入占比图")]


class _VizAPI:
    def __init__(self, payload=b"\x89PNG\r\n\x1a\nfake", fail=False):
        self.calls, self.payload, self.fail = [], payload, fail

    def do(self, method, path, headers=None, raw=False, **kw):
        self.calls.append((method, path, headers, raw))
        if self.fail:
            raise RuntimeError("boom")
        import io
        return {"contents": io.BytesIO(self.payload)}


class _VizClient:
    def __init__(self, **kw):
        self.api_client = _VizAPI(**kw)


def test_chart_png_is_downloaded_as_the_user():
    client = _VizClient()
    png = genie_agent.download_chart(client, "s1", "conv1", "m1", "v1")
    assert png.startswith(b"\x89PNG")
    method, path, headers, raw = client.api_client.calls[0]
    assert (method, raw) == ("GET", True) and headers["Accept"] == "application/octet-stream"
    assert path == "/api/2.0/genie/spaces/s1/conversations/conv1/messages/m1/attachments/v1/download-visualization"


def test_chart_download_failure_returns_none():
    assert genie_agent.download_chart(_VizClient(fail=True), "s1", "conv1", "m1", "v1") is None


def test_uploaded_chart_is_embedded_as_an_image():
    els = genie_agent.report_elements([{"viz": "v1"}], {"v1": "收入占比图"}, images={"v1": "img_v2_1"})
    assert els[0]["tag"] == "img" and els[0]["img_key"] == "img_v2_1"


def test_long_reports_are_split_across_cards_not_truncated(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", HOST)
    paras = [f"段落 {i}：" + "这是一段很长的分析内容，用于测试拆分。" * 40 + f" END{i}" for i in range(30)]
    res = genie_agent.AgentResult(conversation_id="conv1", status="completed",
                                  parts=[{"text": "\n\n".join(paras)}],
                                  queries=[genie_agent.Query("t", "SELECT 1")])
    cards = genie_agent.build_cards(res, "s1")
    assert len(cards) > 1
    blob = "".join(json.dumps(c, ensure_ascii=False) for c in cards)
    assert all(f"END{i}" in blob for i in range(30))          # nothing cut off
    assert all(len(json.dumps(c)) <= genie_agent._CARD_BUDGET for c in cards)
    assert cards[0]["header"]["title"]["content"].endswith(f"（1/{len(cards)}）")
    tags_last = [e.get("tag") for e in cards[-1]["body"]["elements"]]
    assert "collapsible_panel" in tags_last and "button" in tags_last
    assert all("button" not in [e.get("tag") for e in c["body"]["elements"]] for c in cards[:-1])


def test_native_chart_is_built_from_the_charts_query_result():
    res = genie_agent.run_agent(_Client(), "s1", "q", post=_post_returning(_full_stream()))
    chart = genie_agent.native_chart(res, "v1")
    spec = chart["chart_spec"]
    assert chart["tag"] == "chart" and spec["type"] == "bar"
    assert spec["data"]["values"] == [{"x": "Enterprise", "y": 42049561.0}]
    assert spec["title"]["text"] == "收入占比图"


def test_card_draws_a_native_chart_when_no_image_was_uploaded(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", HOST)
    res = genie_agent.run_agent(_Client(), "s1", "q", post=_post_returning(_full_stream()))
    els = [e for c in genie_agent.build_cards(res, "s1") for e in c["body"]["elements"]]
    assert any(e.get("tag") == "chart" for e in els)
    assert not any("请在 Genie 中查看" in str(e.get("content", "")) for e in els)


def test_card_prefers_genies_uploaded_image_over_a_native_chart(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", HOST)
    res = genie_agent.run_agent(_Client(), "s1", "q", post=_post_returning(_full_stream()))
    els = [e for c in genie_agent.build_cards(res, "s1", images={"v1": "img_1"}) for e in c["body"]["elements"]]
    assert any(e.get("tag") == "img" for e in els) and not any(e.get("tag") == "chart" for e in els)


def test_dates_become_a_line_chart():
    spec = genie_agent._chart_spec("月度收入", ["month", "revenue"], [["2026-01", "10"], ["2026-02", "12.5"]])
    assert spec["type"] == "line" and spec["data"]["values"][1] == {"x": "2026-02", "y": 12.5}


def test_plotted_value_follows_the_chart_title():
    spec = genie_agent._chart_spec("Resolution Time by Priority", ["priority", "total_tickets", "avg_resolution_days"],
                                   [["Critical", "124", "1.9"], ["Low", "797", "9.5"]])
    assert [v["y"] for v in spec["data"]["values"]] == [1.9, 9.5]


def test_many_categories_use_horizontal_bars():
    rows = [[f"product {i}", str(i)] for i in range(12)]
    spec = genie_agent._chart_spec("Top products", ["product", "revenue"], rows)
    assert spec["type"] == "bar" and spec["direction"] == "horizontal"


def test_no_numeric_column_means_no_native_chart():
    assert genie_agent._chart_spec("t", ["a", "b"], [["x", "y"]]) is None
