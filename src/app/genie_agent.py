"""Genie **Agent mode** — the multi-step analysis the Genie UI runs — over its
streaming API, rendered as a Lark card.

`POST /api/2.0/genie/agents/{space_id}/responses` streams Server-Sent Events: the
agent's reasoning, each SQL statement it runs (`function_call` named `execute_sql`),
each result, charts (`generate_visualization`), and a final markdown report
(`message`). The call runs as the asking user (their OBO token, OAuth scope `genie`).
Agent mode is Beta: a workspace without the preview answers 404, and the bot then
falls back to chat mode (`app.genie`).
"""
import json
import os
import re
import time
from dataclasses import dataclass, field

from app import genie

AGENT_TIMEOUT_S = int(os.environ.get("GENIE_AGENT_TIMEOUT_S", "600"))
_READ_TIMEOUT_S = 300      # longest silence tolerated between streamed events
_MAX_TABLES = 3            # native table components per card; later tables become bullets
_MAX_TABLE_ROWS = 50
_MAX_SQL_SHOWN = 5
# Lark caps a card message at ~30 KB, so long reports are split across several cards.
# Sizes are measured as escaped JSON (CJK = 6 bytes) to stay safely under the limit.
_CARD_BUDGET = 20000
_MD_CHUNK = 6000           # max size of one markdown element


class AgentUnavailable(Exception):
    """Agent mode isn't enabled for this workspace (HTTP 404)."""


class AgentAuthError(Exception):
    """The user's token was rejected (HTTP 401/403) — they must re-bind."""


class AgentForbidden(Exception):
    """Genie refused the call (HTTP 403). Usually the user's token lacks the `genie`
    scope — e.g. an app session from before the app's scopes changed, which the
    browser keeps until the user signs out of the app."""

    def __init__(self, detail: str = "", missing_scope: bool = False):
        super().__init__(detail)
        self.missing_scope = missing_scope


class AgentBusy(Exception):
    """Genie is still answering an earlier question in this conversation (HTTP 409)."""


class AgentRateLimited(Exception):
    """Genie is rate-limiting requests (HTTP 429)."""


@dataclass
class Query:
    title: str
    sql: str


@dataclass
class AgentResult:
    conversation_id: str | None = None
    message_id: str | None = None  # the agent turn's message; charts are downloaded from it
    status: str = "in_progress"  # completed | failed | timeout | incomplete
    queries: list = field(default_factory=list)
    parts: list = field(default_factory=list)  # final report: {"text": md} or {"viz": attachment_id}
    viz_titles: dict = field(default_factory=dict)
    viz_queries: dict = field(default_factory=dict)  # chart attachment_id -> the SQL call it plots
    results: dict = field(default_factory=dict)      # SQL call_id -> its result, as a markdown table
    error: str | None = None


def _post(url, **kwargs):
    import requests
    return requests.post(url, **kwargs)


def run_agent(client, space_id: str, question: str, conversation_id: str | None = None,
              on_progress=None, timeout_s: int | None = None, post=_post) -> AgentResult:
    """Ask `question` in Agent mode as the user behind `client` (a WorkspaceClient),
    continuing `conversation_id` if given. `on_progress(n_queries, title)` fires as each
    SQL step finishes. Raises the exceptions above for HTTP errors."""
    timeout_s = AGENT_TIMEOUT_S if timeout_s is None else timeout_s
    headers = dict(client.config.authenticate())
    headers.update({"Content-Type": "application/json", "Accept": "text/event-stream"})
    body = {"input": [{"type": "message", "role": "user",
                       "content": [{"type": "input_text", "text": question}]}],
            "enable_viz": True}  # charts stay on the conversation behind the Genie link
    if conversation_id:
        body["conversation_id"] = conversation_id
    url = f"{client.config.host.rstrip('/')}/api/2.0/genie/agents/{space_id}/responses"
    resp = post(url, headers=headers, json=body, stream=True, timeout=(10, _READ_TIMEOUT_S))
    try:
        _check(resp)
        return _consume(resp.iter_lines(), conversation_id, on_progress, time.time() + timeout_s)
    finally:
        close = getattr(resp, "close", None)
        if close:
            close()


def _check(resp) -> None:
    code = resp.status_code
    if code == 200:
        return
    detail = f"HTTP {code}: {(getattr(resp, 'text', '') or '')[:300]}"
    if code == 404:
        raise AgentUnavailable(detail)
    if code == 401:
        raise AgentAuthError(detail)
    if code == 403:
        raise AgentForbidden(detail, missing_scope="scope" in detail.lower())
    if code == 409:
        raise AgentBusy(detail)
    if code == 429:
        raise AgentRateLimited(detail)
    raise RuntimeError(f"Genie Agent mode {detail}")


def iter_events(lines):
    """JSON payloads of the stream's `data:` lines. Lines are raw bytes split on
    newlines only and decoded as UTF-8 here: the stream declares no charset, and
    text-decoding it garbles Chinese and splits lines inside JSON strings."""
    for raw in lines:
        line = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else raw
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            continue


def _consume(lines, conversation_id, on_progress, deadline) -> AgentResult:
    res = AgentResult(conversation_id=conversation_id)
    for ev in iter_events(lines):
        kind = ev.get("type")
        response = ev.get("response") or {}
        if kind == "response.created":
            res.conversation_id = response.get("conversation_id") or res.conversation_id
        elif kind == "response.output_item.done":
            _take(res, ev.get("item") or {}, on_progress)
        elif kind == "response.completed":
            res.status = "completed"
            return res
        elif kind == "response.failed":
            err = response.get("error") or {}
            res.status = "failed"
            res.error = (err.get("message") if isinstance(err, dict) else None) or str(err) or "failed"
            return res
        if time.time() > deadline:
            res.status = "timeout"
            return res
    res.status = "incomplete"
    return res


def _take(res: AgentResult, item: dict, on_progress) -> None:
    kind = item.get("type")
    res.message_id = res.message_id or (item.get("metadata") or {}).get("message_id")
    if kind == "function_call":
        try:
            args = json.loads(item.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        if args.get("sql"):
            res.queries.append(Query(args.get("title") or "", args["sql"]))
            if on_progress:
                try:
                    on_progress(len(res.queries), args.get("title") or "")
                except Exception as e:  # noqa: BLE001 — a progress hiccup must not stop the answer
                    print(f"[agent] progress update failed: {e}", flush=True)
        elif item.get("name") == "generate_visualization":
            viz_id = item.get("id") or item.get("call_id")
            res.viz_titles[viz_id] = args.get("title") or ""
            if args.get("query_attachment_id"):
                res.viz_queries[viz_id] = args["query_attachment_id"]
    elif kind == "function_call_output":
        if item.get("call_id") and item.get("output"):
            res.results[item["call_id"]] = item["output"]
    elif kind == "message":
        parts = []
        for c in item.get("content") or []:
            viz = ((c.get("metadata") or {}).get("viz") or {}).get("attachment_id")
            if c.get("text"):
                parts.append({"text": c["text"]})
            elif viz:
                parts.append({"viz": viz})
        if parts:
            res.parts = parts  # the latest message is the final report


# --- charts --------------------------------------------------------------------

def charts(res: AgentResult) -> list:
    """`(attachment_id, title)` for each chart in the final report, in order."""
    seen, out = set(), []
    for part in res.parts:
        viz = part.get("viz")
        if viz and viz not in seen:
            seen.add(viz)
            out.append((viz, res.viz_titles.get(viz) or ""))
    return out


def download_chart(client, space_id: str, conversation_id: str, message_id: str, attachment_id: str):
    """Genie's rendered PNG of one chart, fetched as the user; None if unavailable."""
    name = f"spaces/{space_id}/conversations/{conversation_id}/messages/{message_id}/attachments/{attachment_id}"
    try:
        res = client.api_client.do("GET", f"/api/2.0/genie/{name}/download-visualization",
                                   headers={"Accept": "application/octet-stream"}, raw=True)
        data = res["contents"].read()
        return data or None
    except Exception as e:  # noqa: BLE001 — a missing chart must not lose the answer
        print(f"[agent] chart download failed for {attachment_id}: {e}", flush=True)
        return None


_DATE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?([T ].*)?$")
_TIME_WORDS = ("date", "month", "week", "year", "day", "quarter", "日期", "月", "周", "年", "季度")
_MAX_CHART_POINTS = 30
_HORIZONTAL_AFTER = 8


def native_chart(res: AgentResult, viz_id: str):
    """A Lark chart drawn from the query result behind one of Genie's charts — used
    when Genie's own image can't be uploaded to Lark. None if the data doesn't chart."""
    result = res.results.get(res.viz_queries.get(viz_id))
    if not result:
        return None
    table = next((payload for kind, payload in _split_tables(result) if kind == "table"), None)
    spec = _chart_spec(res.viz_titles.get(viz_id) or "", *table) if table else None
    return {"tag": "chart", "chart_spec": spec} if spec else None


def _num(value):
    try:
        return float(str(value).replace("**", "").replace(",", "").replace("$", "").replace("%", "").strip())
    except ValueError:
        return None


def _chart_spec(title: str, header: list, rows: list):
    """VChart spec: dates → line, few categories → bars, many → horizontal bars. The
    plotted column is the numeric one whose name best matches the chart title."""
    rows = [r for r in rows if len(r) == len(header)][:_MAX_CHART_POINTS]
    if not rows or len(header) < 2:
        return None
    numeric = [i for i in range(len(header)) if all(_num(r[i]) is not None for r in rows)]
    category = next((i for i in range(len(header)) if i not in numeric), 0)
    values = [i for i in numeric if i != category]
    if not values:
        return None
    words = set(re.findall(r"[a-z]+", title.lower()))
    y = max(values, key=lambda i: (len(set(header[i].lower().split("_")) & words), -values.index(i)))
    data = [{"x": str(r[category]).replace("**", ""), "y": _num(r[y])} for r in rows]
    is_time = (any(w in header[category].lower() for w in _TIME_WORDS)
               or all(_DATE.match(d["x"]) for d in data))
    spec = {"title": {"visible": bool(title), "text": title}, "data": {"values": data}}
    if is_time:
        spec.update(type="line", xField="x", yField="y", point={"visible": True})
    elif len(data) > _HORIZONTAL_AFTER:
        spec.update(type="bar", direction="horizontal", xField="y", yField="x", label={"visible": True})
    else:
        spec.update(type="bar", xField="x", yField="y", label={"visible": True})
    return spec


# --- rendering ---------------------------------------------------------------

_SUPERSCRIPT = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")
_CITATION = re.compile(r"\\\[\[(\d+)\]\((\S+?)\)\\\]")
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.M)
_TABLE_SEP = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?$")
_SCI = re.compile(r"^-?\d+(\.\d+)?[eE][+-]?\d+$")


def lark_markdown(md: str) -> str:
    """Genie's markdown → what a Lark card renders: headings become bold lines, and
    `\\[[n](url)\\]` citations become compact superscript links into Genie."""
    md = _CITATION.sub(lambda m: f"[{m.group(1).translate(_SUPERSCRIPT)}]({m.group(2)})", md)
    md = md.replace("\\[", "[").replace("\\]", "]")
    md = _HEADING.sub(lambda m: "**" + m.group(1).replace("**", "").strip() + "**", md)
    return md.strip()


def report_elements(parts: list, viz_titles: dict, images: dict | None = None,
                    charts: dict | None = None) -> list:
    """The final report as card elements — markdown, native tables, and charts: Genie's
    own image when `images` has its Lark image_key, else a native chart from `charts`,
    else a note. Nothing is truncated: `build_cards` spreads a long report over cards."""
    images, charts = images or {}, charts or {}
    elements, tables = [], 0
    for part in parts:
        if "viz" in part:
            title = viz_titles.get(part["viz"]) or "图表"
            key = images.get(part["viz"])
            if key:
                elements.append({"tag": "img", "img_key": key, "scale_type": "fit_horizontal",
                                 "preview": True, "alt": {"tag": "plain_text", "content": title}})
            elif charts.get(part["viz"]):
                elements.append(charts[part["viz"]])
            else:
                elements.append(genie._md(f"📊 图表「{title}」请在 Genie 中查看"))
            continue
        for kind, payload in _split_tables(part.get("text") or ""):
            if kind == "md":
                md = lark_markdown(payload)
                elements += [genie._md(chunk) for chunk in _split_md(md)] if md else []
            elif tables < _MAX_TABLES:
                elements.append(_table(*payload))
                tables += 1
            else:
                elements.append(genie._md(_table_bullets(*payload)))
    return elements


def _json_size(value) -> int:
    return len(json.dumps(value))


def _split_md(md: str, limit: int = _MD_CHUNK) -> list:
    """Split markdown into chunks under `limit` (escaped JSON size) at paragraph, then
    line boundaries; only a single overlong line is cut mid-text."""
    if _json_size(md) <= limit:
        return [md]
    pieces = []
    for para in md.split("\n\n"):
        if _json_size(para) <= limit:
            pieces.append(para)
            continue
        for line in para.split("\n"):
            while _json_size(line) > limit:
                cut = max(1, len(line) * limit // (2 * _json_size(line)))
                pieces.append(line[:cut])
                line = line[cut:]
            pieces.append(line)
    chunks, cur = [], ""
    for piece in pieces:
        candidate = f"{cur}\n\n{piece}" if cur else piece
        if cur and _json_size(candidate) > limit:
            chunks.append(cur)
            cur = piece
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    return chunks


def _split_tables(text: str) -> list:
    """Split markdown into ("md", str) and ("table", (header, rows)) segments, in order.
    Genie sometimes glues the next paragraph onto a table's last row
    (`| Low | 9.5 |**Key findings:**`); that text is split back off."""
    lines = text.split("\n")
    out, buf, i = [], [], 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("|") and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1].strip()):
            if buf:
                out.append(("md", "\n".join(buf)))
                buf = []
            header, rows, i = _cells(line), [], i + 2
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                row = lines[i].rstrip()
                end = row.rfind("|")
                rows.append(_cells(row[:end + 1]))
                i += 1
                if row[end + 1:].strip():
                    buf.append(row[end + 1:].strip())
                    break
            out.append(("table", (header, rows)))
            continue
        buf.append(line)
        i += 1
    if buf:
        out.append(("md", "\n".join(buf)))
    return out


def _cells(line: str) -> list:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _table(header: list, rows: list) -> dict:
    names = [f"c{i}" for i in range(len(header))]
    money = [any(h in col.lower() for h in genie._MONEY_HINTS) for col in header]

    def cell(i, value):
        v = value.replace("**", "").replace("`", "").strip()
        if _SCI.match(v):  # e.g. 4.2049561E7 → $4,205万 / 42,049,561
            return genie._fmt_money(v) if money[i] else genie._fmt_val(v)
        return v

    return {
        "tag": "table",
        "page_size": min(10, max(1, len(rows))),
        "row_height": "low",
        "header_style": {"bold": True, "background_style": "grey"},
        "columns": [{"name": n, "display_name": h.replace("_", " "), "data_type": "text", "width": "auto"}
                    for n, h in zip(names, header)],
        "rows": [{n: cell(i, r[i]) if i < len(r) else "" for i, n in enumerate(names)}
                 for r in rows[:_MAX_TABLE_ROWS]],
    }


def _table_bullets(header: list, rows: list) -> str:
    lines = []
    for r in rows:
        rest = " · ".join(f"{h.replace('_', ' ')} {v}" for h, v in zip(header[1:], r[1:]))
        lines.append(f"- **{r[0] if r else ''}**" + (f" — {rest}" if rest else ""))
    return "\n".join(lines)


def _sql_panel(queries: list) -> dict:
    shown = queries[:_MAX_SQL_SHOWN]
    body = "\n\n".join(f"**{i}. {q.title or '查询'}**\n{genie._compact_sql(q.sql)}"
                       for i, q in enumerate(shown, 1))
    if len(queries) > len(shown):
        body += f"\n\n…另有 {len(queries) - len(shown)} 条，请在 Genie 中查看"
    return {"tag": "collapsible_panel", "expanded": False,
            "header": {"title": genie._md(f"**执行的 SQL（{len(queries)} 条）**")},
            "elements": [genie._md(body)]}


def build_cards(res: AgentResult, space_id: str, images: dict | None = None) -> list:
    """Cards for an Agent-mode answer: the report (with charts), then — on the last
    card — the SQL it ran and a link to the conversation in Genie. A long report spans
    several cards, numbered （1/N）, instead of being cut off."""
    link = genie.conversation_url(space_id, res.conversation_id)
    images = images or {}
    fallback = {viz: native_chart(res, viz) for viz, _ in charts(res) if viz not in images}
    body = report_elements(res.parts, res.viz_titles, images, fallback)
    if res.status == "timeout":
        body.insert(0, genie._md("⏳ 分析耗时较长，Genie 仍在继续——请点击下方按钮在 Genie 中查看完整结果。"))
    elif res.status != "completed":
        body.insert(0, genie._md(f"⚠️ 分析未能完成（{res.error or res.status}）。"))
    elif not body:
        body.append(genie._md("抱歉，未能获取到答案。"))
    tail = ([_sql_panel(res.queries)] if res.queries else []) + ([genie.genie_button(link)] if link else [])

    pages, cur = [], []
    for el in body:
        if cur and _card_size(cur + [el]) > _CARD_BUDGET:
            pages.append(cur)
            cur = []
        cur.append(el)
    if tail and cur and _card_size(cur + tail) > _CARD_BUDGET:
        pages.append(cur)
        cur = []
    pages.append(cur + tail)

    cards = [genie.card_shell(p) for p in pages]
    if len(cards) > 1:
        for i, card in enumerate(cards, 1):
            card["header"]["title"]["content"] += f"（{i}/{len(cards)}）"
    return cards


def _card_size(elements: list) -> int:
    return _json_size(genie.card_shell(elements)) + 60  # headroom for the （i/N） title suffix
