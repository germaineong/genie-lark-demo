from app import genie


def test_fmt_money_uses_wan_for_large_amounts():
    assert genie._fmt_money("42049560.7") == "$4,205万"


def test_fmt_money_uses_yi_for_very_large():
    assert genie._fmt_money("420495607") == "$4.2亿"


def test_fmt_money_small_amounts_plain():
    assert genie._fmt_money("250") == "$250"


def test_fmt_pct_one_decimal_with_sign():
    assert genie._fmt_pct("37.36") == "37.4%"


class _SegAPI:
    def do(self, method, path, body=None):
        if path.endswith("/start-conversation"):
            return {"conversation_id": "c", "message_id": "m"}
        if "/attachments/" in path and path.endswith("/query-result"):
            return {"statement_response": {
                "manifest": {"schema": {"columns": [
                    {"name": "segment"}, {"name": "revenue"}, {"name": "pct_of_total_revenue"}]}},
                "result": {"data_array": [
                    ["Enterprise", "42049560.7", "37.36"],
                    ["SMB", "32914041.75", "29.25"]]}}}
        return {"status": "COMPLETED", "attachments": [
            {"attachment_id": "a1", "query": {
                "description": "各客户细分市场占比",
                "query": "SELECT `c`.`segment` FROM `cat`.`sch`.`orders` `o`"}}]}


class _SegClient:
    api_client = _SegAPI()


def test_card_tiles_formatted_and_no_raw():
    blob = str(genie.ask_genie(_SegClient(), "s", "q")["body"]["elements"])
    assert "$4,205万" in blob            # revenue formatted like Genie
    assert "37.4%" in blob               # percentage formatted
    assert "Enterprise" in blob          # tile label
    assert "42,049,560" not in blob      # no raw float
    assert "pct of total revenue" not in blob  # verbose column label dropped


def test_chart_spec_is_donut_over_segments():
    els = genie.ask_genie(_SegClient(), "s", "q")["body"]["elements"]
    chart = next(e for e in els if e.get("tag") == "chart")
    spec = chart["chart_spec"]
    assert spec["type"] == "pie" and spec.get("innerRadius", 0) > 0  # donut
    cats = {v["type"] for v in spec["data"]["values"]}
    assert {"Enterprise", "SMB"} <= cats
