"""Genie **chat mode** (the Conversation API) and the shared Lark **card 2.0** pieces.

Chat mode is the bot's fallback: the default is Agent mode (`app.genie_agent`), the
multi-step analysis the Genie UI runs. The Databricks client is **passed in**
(per-user, from the bound OBO token), so Genie runs the SQL under the asking user's
identity. Follow-ups post into the same conversation so Genie keeps the context.

The chat card shows Genie's own answer, then KPI tiles (`column_set`), a donut
`chart`, the executed SQL in a `collapsible_panel`, suggested follow-up questions, and
a 「在Genie中查看」 button into the conversation. Money renders as `$4,205万` /
`$4.2亿`, percentages as `37.4%`. If Lark rejects a card (e.g. an unsupported chart
or table), `strip_rich()` yields a plain copy that the bot resends (see bot.py).
"""
import os
import re
import time

GENIE_TIMEOUT_S = int(os.environ.get("GENIE_TIMEOUT_S", "150"))
PREVIEW_ROWS = 10
_TILE_MAX_ROWS = 4      # render KPI tiles for small results, else a bullet list
_CHART_MAX_ROWS = 8     # donut only makes sense for a handful of slices
_SQL_MAX_CHARS = 800
_DEFAULT_TITLE = "Genie 数据分析助手"  # card header; override per deployment with BOT_TITLE

_ISO = re.compile(r"^(\d{4}-\d{2}-\d{2})T[\d:.]+Z?$")
_MONEY_HINTS = ("成本", "金额", "美元", "营收", "收入", "销售", "营业额",
                "revenue", "sales", "cost", "price", "amount", "usd", "gmv")
_PCT_HINTS = ("pct", "percent", "占比", "比例", "ratio", "share", "rate")
_SQL_KW = re.compile(
    r"\s+(?=(?:SELECT|FROM|WHERE|GROUP BY|ORDER BY|HAVING|LIMIT|LEFT JOIN|"
    r"RIGHT JOIN|INNER JOIN|JOIN|ON|UNION ALL|UNION|WITH)\b)",
    re.IGNORECASE,
)


def conversation_url(space_id: str, conversation_id: str | None = None):
    """Genie UI link to a space, or to one conversation in it (where Agent mode's
    charts and full report live). None when DATABRICKS_HOST isn't set."""
    host = os.environ.get("DATABRICKS_HOST", "").strip().rstrip("/")
    if not host:
        return None
    if not host.startswith("http"):
        host = "https://" + host
    if not conversation_id:
        return f"{host}/genie/rooms/{space_id}"
    workspace_id = os.environ.get("DATABRICKS_WORKSPACE_ID")
    return f"{host}/genie/rooms/{space_id}/chats/{conversation_id}" + (f"?o={workspace_id}" if workspace_id else "")


def ask_genie(client, space_id: str, question: str) -> dict:
    """One chat-mode question → Lark card (see `ask_genie_chat`)."""
    return ask_genie_chat(client, space_id, question)[0]


def ask_genie_chat(client, space_id: str, question: str, conversation_id: str | None = None):
    """Ask in chat mode using `client` (a WorkspaceClient with `.api_client.do`). With
    `conversation_id` the question is a follow-up in that conversation, so Genie keeps
    the context. Returns `(card, conversation_id)`."""
    def _do(method: str, path: str, body=None) -> dict:
        return client.api_client.do(method, path, body=body)

    base = f"/api/2.0/genie/spaces/{space_id}"
    if conversation_id:
        sent = _do("POST", f"{base}/conversations/{conversation_id}/messages", {"content": question})
        conv_id, msg_id = conversation_id, sent.get("message_id") or sent.get("id")
    else:
        start = _do("POST", f"{base}/start-conversation", {"content": question})
        conv_id, msg_id = start["conversation_id"], start["message_id"]
    genie_url = conversation_url(space_id, conv_id)

    deadline = time.time() + GENIE_TIMEOUT_S
    message: dict = {}
    while time.time() < deadline:
        message = _do("GET", f"{base}/conversations/{conv_id}/messages/{msg_id}")
        if message.get("status") in ("COMPLETED", "FAILED", "CANCELLED", "QUERY_RESULT_EXPIRED"):
            break
        time.sleep(2)

    if message.get("status") != "COMPLETED":
        return _build_card(f"查询未成功完成（状态：{message.get('status')}）。", [], [], None, genie_url), conv_id

    answer, description, sql, columns, rows, suggestions = "", "", None, [], [], []
    for att in message.get("attachments", []) or []:
        if att.get("text"):
            answer = att["text"].get("content", "") or answer
        if att.get("suggested_questions"):
            suggestions = att["suggested_questions"].get("questions") or suggestions
        if att.get("query"):
            sql = att["query"].get("query")
            description = att["query"].get("description", "") or description
            res = _do("GET", f"{base}/conversations/{conv_id}"
                            f"/messages/{msg_id}/attachments/{att['attachment_id']}/query-result")
            stmt = res.get("statement_response") or {}
            columns = [c["name"] for c in (stmt.get("manifest") or {}).get("schema", {}).get("columns", [])]
            rows = (stmt.get("result") or {}).get("data_array") or []

    # Genie's written answer wins; the query's one-line description is only a fallback.
    return _build_card(answer or description, columns, rows, sql, genie_url, suggestions), conv_id


# --- value formatting -------------------------------------------------------

def _clean(value) -> str:
    s = str(value)
    m = _ISO.match(s)
    return m.group(1) if m else s


def _fmt_money(value) -> str:
    """Currency, Genie-style: `$4,205万` (万 = 1e4) and `$4.2亿` (亿 = 1e8)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    a = abs(v)
    if a >= 1e8:
        return f"${v / 1e8:,.1f}亿"
    if a >= 1e4:
        return f"${v / 1e4:,.0f}万"
    return f"${v:,.0f}" if v == int(v) else f"${v:,.2f}"


def _fmt_pct(value) -> str:
    """Percentage with one decimal and a sign: `37.4%`."""
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return str(value)


def _fmt_val(value) -> str:
    s = _clean(value)
    try:
        f = float(s)
    except (TypeError, ValueError):
        return s
    return f"{int(f):,}" if f == int(f) else f"{f:,.2f}".rstrip("0").rstrip(".")


def _pretty_sql(sql: str) -> str:
    s = " ".join(sql.strip().split())
    s = _SQL_KW.sub("\n", s)
    s = re.sub(r"\s+(?=\b(?:AND|OR)\b)", "\n    ", s, flags=re.IGNORECASE)
    return s


def _compact_sql(sql: str) -> str:
    """Clause-per-line, backticks stripped, truncated if very long."""
    s = _pretty_sql(sql).replace("`", "")
    if len(s) > _SQL_MAX_CHARS:
        s = s[:_SQL_MAX_CHARS].rstrip() + "\n…（SQL 已截断）"
    return s


# --- card 2.0 construction --------------------------------------------------

def _md(content: str) -> dict:
    return {"tag": "markdown", "content": content}


def card_shell(elements: list) -> dict:
    """A Lark card 2.0 with the bot's header around `elements`."""
    return {
        "schema": "2.0",
        "header": {"template": "blue",
                   "title": {"tag": "plain_text",
                             "content": os.environ.get("BOT_TITLE") or _DEFAULT_TITLE}},
        "body": {"elements": elements},
    }


def genie_button(url: str) -> dict:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": "在Genie中查看"},
        "type": "primary",
        "width": "default",
        "behaviors": [{"type": "open_url", "default_url": url}],
    }


def strip_rich(card: dict) -> dict:
    """Return a plain copy of the card — no chart, tables as text, the button as a
    link. The bot resends this if Lark rejects the full card, so the user still gets
    the answer and SQL."""
    elements = []
    for e in card.get("body", {}).get("elements", []):
        tag = e.get("tag")
        if tag == "chart":
            continue
        if tag == "img":
            title = (e.get("alt") or {}).get("content") or "图表"
            elements.append(_md(f"📊 图表「{title}」请在 Genie 中查看"))
        elif tag == "table":
            elements.append(_md(_table_text(e)))
        elif tag == "button":
            url = next((b.get("default_url") for b in e.get("behaviors") or [] if b.get("default_url")), None)
            if url:
                elements.append(_md(f"[在Genie中查看]({url})"))
        else:
            elements.append(e)
    out = dict(card)
    out["body"] = dict(card.get("body", {}), elements=elements)
    return out


def _table_text(table: dict) -> str:
    cols = table.get("columns") or []
    lines = []
    for row in table.get("rows") or []:
        vals = [str(row.get(c.get("name"), "")) for c in cols]
        if not vals:
            continue
        rest = " · ".join(f"{c.get('display_name', '')} {v}" for c, v in zip(cols[1:], vals[1:]))
        lines.append(f"- **{vals[0]}**" + (f" — {rest}" if rest else ""))
    return "\n".join(lines) or "（表格）"


def _build_card(text: str, columns: list, rows: list, sql: str | None,
                genie_url: str | None = None, suggestions: list | None = None) -> dict:
    elements: list = []
    if text:
        elements.append(_md(text))

    if columns and rows:
        pct = {i for i, c in enumerate(columns) if any(h in c.lower() for h in _PCT_HINTS)}
        money = {i for i, c in enumerate(columns)
                 if i not in pct and any(h in c.lower() for h in _MONEY_HINTS)}

        def cell(i, r):
            if i in pct:
                return _fmt_pct(r[i])
            if i in money:
                return _fmt_money(r[i])
            return _fmt_val(r[i])

        def piece(i, r):
            # money/percent values are self-describing; keep labels only for other cols
            val = cell(i, r)
            return val if (i in money or i in pct) else f"{columns[i].replace('_', ' ')} {val}"

        metric_cols = list(range(1, len(columns)))  # col 0 is the row label
        if 1 <= len(rows) <= _TILE_MAX_ROWS and len(columns) >= 2:
            elements.append(_tiles(columns, rows, cell, piece, metric_cols))
        else:
            elements.append(_md(_bullets(columns, rows, cell, piece)))

        chart = _donut(columns, rows, pct, money)
        if chart:
            elements.append(chart)

    if sql:
        elements.append({
            "tag": "collapsible_panel",
            "expanded": False,
            "header": {"title": _md("**执行的 SQL**")},
            "elements": [_md(_compact_sql(sql))],
        })

    if suggestions:
        elements.append(_md("**你可以接着问：**\n" + "\n".join(f"- {q}" for q in suggestions)))

    if genie_url:
        elements.append(genie_button(genie_url))

    if not elements:
        elements.append(_md("抱歉，未能获取到答案。"))

    return card_shell(elements)


def _tiles(columns, rows, cell, piece, metric_cols) -> dict:
    """One KPI tile per row (segment name + its formatted metrics), side by side."""
    cols = []
    for r in rows:
        lines = [f"**{cell(0, r)}**"] + [piece(i, r) for i in metric_cols]
        cols.append({
            "tag": "column", "width": "weighted", "weight": 1, "vertical_align": "top",
            "elements": [_md("\n".join(lines))],
        })
    return {"tag": "column_set", "flex_mode": "stretch",
            "horizontal_spacing": "default", "columns": cols}


def _bullets(columns, rows, cell, piece) -> str:
    """Fallback for larger results: constant columns collapsed, one line per row."""
    constant = [i for i in range(len(columns)) if len({_clean(r[i]) for r in rows}) == 1]
    varying = [i for i in range(len(columns)) if i not in constant]
    lines = [f"{columns[i].replace('_', ' ')}：**{cell(i, rows[0])}**" for i in constant]
    if constant:
        lines.append("")
    for r in rows[:PREVIEW_ROWS]:
        if not varying:
            break
        metrics = " · ".join(piece(i, r) for i in varying[1:])
        lines.append(f"- **{cell(varying[0], r)}** — {metrics}" if metrics else f"- **{cell(varying[0], r)}**")
    if len(rows) > PREVIEW_ROWS:
        lines.append(f"\n_共 {len(rows)} 行，显示前 {PREVIEW_ROWS} 行_")
    return "\n".join(lines)


def _donut(columns, rows, pct, money):
    """A donut over (category = first column, value = a money/percent/numeric column),
    for a handful of rows. Returns None when the shape doesn't suit a pie."""
    if not (2 <= len(rows) <= _CHART_MAX_ROWS) or len(columns) < 2:
        return None
    val = next((i for i in range(1, len(columns)) if i in money), None)
    if val is None:
        val = next((i for i in range(1, len(columns)) if i in pct), None)
    if val is None:
        for i in range(1, len(columns)):
            try:
                [float(r[i]) for r in rows]
                val = i
                break
            except (TypeError, ValueError):
                continue
    if val is None:
        return None

    values = []
    for r in rows:
        try:
            values.append({"type": str(r[0]), "value": float(r[val])})
        except (TypeError, ValueError):
            return None

    return {"tag": "chart", "chart_spec": {
        "type": "pie",
        "data": {"values": values},
        "valueField": "value",
        "categoryField": "type",
        "outerRadius": 0.8,
        "innerRadius": 0.6,
        "legends": {"visible": True},
        "label": {"visible": True},
    }}
