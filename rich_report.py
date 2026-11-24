"""
结构化报告（目前只有全球科技 tech）：
AI 只写中文标题、介绍、分类、必看；链接、英文原标题、热度、日期、排名由程序从抓取结果里照抄，
这样 AI 编不出假链接和假数字。
结果存成 reports/<bot>/日期.json（网站用），同时生成 Markdown（RSS、Issue 用）。
"""
import json
import re
from collections import Counter
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone

JSON_BOTS = {"tech", "politics", "finance", "reddit", "music", "commerce", "chiangmai", "events", "releases"}
CST = timezone(timedelta(hours=8))

RANKINGS = {
    # (榜单组：条目的 rank_group，没有就看 source；榜单名；排名前缀；最多几条)
    "tech": [("GitHub Trending", "GitHub Trending 今日飙升", "GH", 10), ("Hacker News", "Hacker News 热榜", "HN", 8)],
    "reddit": [("Reddit", "Reddit 今日热帖 Top 10", "#", 10)],
    "commerce": [("Reddit", "Reddit 社区热帖 Top 8", "#", 8)],
    "finance": [("Polymarket", "Polymarket 预测市场热门", "PM", 8)],
}
MUST_TITLE = {"chiangmai": "本周必去 {n} 条", "events": "本周 {n} 条必看", "releases": "本周 {n} 条必听"}
TERMS_TITLE = {"chiangmai": "本周英文小词"}
TICKERS_ZH = {"S&P 500": "标普 500", "Nasdaq": "纳斯达克", "US 10Y Yield": "美国 10 年期国债收益率", "US Dollar Index": "美元指数",
              "Gold Futures": "黄金期货", "WTI Crude Oil": "WTI 原油", "Bitcoin": "比特币"}


def parse_categories(prompt: str) -> list:
    """从 prompts/<bot>.md 的【分类】段落读出分类名（每行「- 名称：说明」）。"""
    m = re.search(r"【分类】[^\n]*\n((?:[ \t]*-[^\n]*\n?)+)", prompt)
    cats = []
    for line in (m.group(1).splitlines() if m else []):
        name = re.split(r"[：:]", line.strip().lstrip("-").strip(), 1)[0].strip()
        if name:
            cats.append(name)
    return cats or ["精选"]

JSON_FORMAT = """
【输出格式：只输出一个 JSON 对象，不要任何别的文字，不要 ```】
{
  "overview": "一两句话：今天最值得注意的几件事（中文）",
  "items": [
    {"id": 抓取结果里的编号（数字）,
     "source": "信源：原始发布方的名字，参考 Publisher；没有 Publisher 就用方括号里的来源名。例如 CNBC、Z.ai、GitHub Trending、Hacker News",
     "category": "分类，从规定的分类里原样照抄",
     "title_zh": "中文标题，格式「主体 + 动作：要点」，15～30 字，例如「智谱发布 GLM-5.4：首次用循环变换器做更通用的编程」",
     "intro": "3～5 句中文介绍：是什么、有什么用或为什么重要、关键数字。术语第一次出现写成 English（中文解释）",
     "when": "只有活动/演出类才填：日期 · 时间 · 场地 · 票价（抓取结果里有的才写）；其他类型填空字符串",
     "must": 0,
     "must_text": "只有必看条目才写：一句话（40～70 字）讲清发生了什么和关键数字；非必看填空字符串"}
  ],
  "rank_notes": [
    {"id": 编号, "title_zh": "中文标题", "brief": "一句话中文简介（20～40 字）。不要写名次和热度。GitHub 项目以「语言 + 项目，」开头，例如「Python 项目，……」"}
  ],
  "terms": [{"term": "English（中文）", "explain": "两三句大白话解释"}]
}
【规则】
- 标有「Paywall: 需付费订阅」的条目：同一件事有免费来源报道时，一定选免费来源那条；只有付费来源报道的重要新闻才选付费那条。
- items 里的 id 必须是抓取结果里真实存在的编号；同一件事被多个来源报道时只选一条（优先选原始发布方），其余不要。
- must 字段：选出 5 条今日必看，分别填 1～5（1 最重要），其余都填 0。
- 每条 intro 只能根据该条的 Summary、Article lead 写，不要加入抓取结果里没有的事实和数字。
- rank_notes：抓取结果里所有带「Rank:」的条目都要写一条，不管 items 里有没有选它；没有带 Rank 的条目就输出空数组。
- terms：挑 1～3 个今天出现的新概念。
- 中文和英文、数字之间加一个空格，例如「发布 GLM-5.4 模型」「264 亿参数」。
- 读者是中文用户，不懂英文。全部用平实、句子短的简体中文。不用网络烂梗（炸裂、王炸、遥遥领先等）。
- 个人或公司自己声称的收入、利润、用户数，标注【自称，未验证】；票价、商品价格、官方统计数据、市场行情不要标。
- source 只写名字本身，例如「AFP」「r/ableton」，不要写「信源：」之类的前缀。
"""

FALLBACK_FORMAT = """
【输出格式】
## 今日 5 条必看
5 条，每条单独一行、以“- ”开头，格式：**【信源】[中文标题](URL)**：用一两句话讲清是什么、为什么值得看。

## 一手精选速览
按上面规定的分类分组（### 分类名），每个分类 5～8 条，每条以“- ”开头，格式：**【信源】[中文标题](URL)**：3～5 句介绍，最后写热度。不要重复必看里的。

## 今日新词
挑 1～3 个新概念，各用两三句大白话解释。
"""


def _extract_json(text: str) -> dict:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b < a:
        raise ValueError("AI 返回里找不到 JSON")
    return json.loads(t[a:b + 1])


def _short_date(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw)
    try:
        if m and "T" in raw:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00")[:19]).replace(tzinfo=timezone.utc).astimezone(CST)
            return dt.strftime("%m-%d")
        if m:
            return f"{m.group(2)}-{m.group(3)}"
        return parsedate_to_datetime(raw).astimezone(CST).strftime("%m-%d")
    except Exception:
        return ""


def _clean_en(title: str) -> str:
    return re.sub(r"^\[(Paper|Model|Space|Dataset)\]\s*", "", title or "").strip()


def build(bot, today, title, prompt, user, fetched, ask):
    cats = parse_categories(prompt)
    by_id = {it["id"]: it for it in fetched["items"] if "id" in it}
    system = prompt + "\n" + JSON_FORMAT

    data, err = None, None
    for attempt in range(2):
        msg = user if attempt == 0 else user + f"\n\n（上一次输出无法解析：{err}。这次只输出合法 JSON，items 最多 30 条，intro 每条 3 句。）"
        raw = ask(system, msg)
        try:
            data = _extract_json(raw)
            if not isinstance(data.get("items"), list) or not data["items"]:
                raise ValueError("没有 items")
            break
        except Exception as e:
            err = str(e)[:120]
            print(f"[WARN] 第 {attempt + 1} 次 JSON 解析失败：{err}；开头：{raw[:200]!r}")
            data = None
    if data is None:
        raise ValueError(err)

    items, seen = [], set()
    for x in data["items"]:
        try:
            fid = int(x.get("id"))
        except (TypeError, ValueError):
            continue
        src = by_id.get(fid)
        if not src or fid in seen or not str(x.get("title_zh", "")).strip():
            continue
        seen.add(fid)
        cat = str(x.get("category", "")).strip()
        if cat not in cats:
            cat = next((c for c in cats if c[:2] in cat), cats[0])
        try:
            must = int(x.get("must") or 0)
        except (TypeError, ValueError):
            must = 0
        items.append({
            "id": fid,
            "source": re.sub(r"^(信源|来源|source)\s*[:：]\s*", "", str(x.get("source") or src.get("publisher") or src["source"]).strip(), flags=re.I)[:40],
            "via": src["source"],
            "category": cat,
            "title_zh": str(x["title_zh"]).strip(),
            "title_en": _clean_en(src.get("title", "")),
            "url": src.get("url", ""),
            "discuss": src.get("discuss", ""),
            "paywall": bool(src.get("paywall")),
            "intro": str(x.get("intro", "")).strip(),
            "heat": src.get("heat_text", ""),
            "date": _short_date(src.get("published", "")),
            "must": must if 1 <= must <= 5 else 0,
            "must_text": str(x.get("must_text") or "").strip(),
            "when": str(x.get("when") or "").strip(),
        })
    if not items:
        raise ValueError("没有有效条目")

    # 必看：按 1～5 排；不够 5 条就用前面的补齐，多了就截断
    musts = sorted([i for i in items if i["must"]], key=lambda i: i["must"])[:5]
    for i in items:
        i["must"] = 0
    for i in items:
        if len(musts) >= 5:
            break
        if i not in musts:
            musts.append(i)
    for k, i in enumerate(musts, 1):
        i["must"] = k

    notes = {}
    for n in data.get("rank_notes") or []:
        try:
            notes[int(n.get("id"))] = n
        except (TypeError, ValueError, AttributeError):
            pass
    picked = {i["id"]: i for i in items}
    rankings = []
    for src_name, label, prefix, top in RANKINGS.get(bot, []):
        rows = []
        for it in sorted((v for v in by_id.values() if v.get("rank_group", v["source"]) == src_name and v.get("rank")), key=lambda v: v["rank"])[:top]:
            n, p = notes.get(it["id"], {}), picked.get(it["id"], {})
            rows.append({
                "rank": f"{prefix} {it['rank']}",
                "source": it["source"],
                "title_zh": str(n.get("title_zh") or p.get("title_zh") or "").strip(),
                "title_en": _clean_en(it.get("title", "")),
                "url": it.get("url", ""),
                "discuss": it.get("discuss", ""),
                "paywall": bool(it.get("paywall")),
                "brief": str(n.get("brief") or "").strip(),
                "heat": it.get("heat_text", ""),
            })
        if rows:
            rankings.append({"name": label, "rows": rows})

    snapshot = []
    if bot == "finance":
        tick = next((v for v in by_id.values() if v.get("title", "").startswith("Live Global Macro Tickers")), None)
        for part in (tick or {}).get("summary", "").split("|"):
            m = re.match(r"\s*(.+?):\s*([\d,.\-]+)\s*\(([-+]?[\d.]+%)\)", part)
            if m:
                snapshot.append({"name": TICKERS_ZH.get(m.group(1).strip(), m.group(1).strip()), "value": m.group(2), "change": m.group(3)})

    pubs = [s for s, _ in Counter(i["source"] for i in items).most_common()]
    failed = [s["source"] for s in fetched.get("source_stats", []) if not s.get("count")]
    summary = f"汇总 {'、'.join(pubs[:14])}{' 等' if len(pubs) > 14 else ''}信源共 {len(items)} 条精选。"
    if str(data.get("overview", "")).strip():
        summary += str(data["overview"]).strip()
    locked = sum(1 for i in items if i.get("paywall"))
    if locked:
        summary += f" 标 🔒 的 {locked} 条需要付费订阅才能看全文。"
    if failed:
        summary += f" 今日 {'、'.join(failed)} 没有返回内容。"
    if rankings:
        summary += " 最下方是 " + " 和 ".join(f"{r['name']}前 {len(r['rows'])}" for r in rankings) + "。"

    terms = [t for t in (data.get("terms") or []) if isinstance(t, dict) and t.get("term")][:3]
    order = {c: k for k, c in enumerate(cats)}
    items.sort(key=lambda i: order.get(i["category"], 9))
    return {
        "version": 1, "bot": bot, "date": today, "title": title, "summary": summary,
        "categories": cats, "items": items, "rankings": rankings, "terms": terms, "snapshot": snapshot,
        "must_title": MUST_TITLE.get(bot, "今日 {n} 条必看"), "terms_title": TERMS_TITLE.get(bot, "今日新词"),
        "sources_total": fetched.get("sources_total"), "fetched_total": fetched.get("total_items"),
    }


def _md_link(title, url, paywall=False):
    t = title.replace("[", "【").replace("]", "】")
    lock = " 🔒" if paywall else ""
    return (f"[{t}]({url})" if url else t) + lock


def to_markdown(r):
    musts = sorted([i for i in r["items"] if i["must"]], key=lambda i: i["must"])
    out = [r["summary"], "", "## " + r.get("must_title", "今日 {n} 条必看").replace("{n}", str(len(musts)))]
    for i in musts:
        when = f"（{i['when']}）" if i.get("when") else ""
        out.append(f"- **【{i['source']}】{_md_link(i['title_zh'], i['url'], i.get('paywall'))}**{when}：{i.get('must_text') or i['intro']}")
    if r.get("snapshot"):
        out += ["", "## 市场快照", "", "| 资产 | 最新 | 涨跌 |", "|---|---|---|"] + [f"| {x['name']} | {x['value']} | {x['change']} |" for x in r["snapshot"]]
    out += ["", "## 一手精选速览"]
    for c in r["categories"]:
        rows = [i for i in r["items"] if i["category"] == c and not i["must"]]
        if not rows:
            continue
        out += ["", f"### {c}（{len(rows)}）"]
        for i in rows:
            tail = " · ".join(x for x in (i.get("when", ""), i["heat"], i["date"]) if x)
            out.append(f"- **【{i['source']}】{_md_link(i['title_zh'], i['url'], i.get('paywall'))}**（{i['title_en']}）：{i['intro']}" + (f"　*{tail}*" if tail else ""))
    for rk in r["rankings"]:
        out += ["", f"## {rk['name']}", "", "| 排名 | 信源 | 标题 | 简介 | 热度 |", "|---|---|---|---|---|"]
        for row in rk["rows"]:
            cell = lambda s: str(s).replace("|", "/").replace("\n", " ")
            out.append(f"| {row['rank']} | {cell(row['source'])} | {cell(_md_link(row['title_zh'] or row['title_en'], row['url'], row.get('paywall')))} | {cell(row['brief'])} | {cell(row['heat'])} |")
    if r["terms"]:
        out += ["", f"## {r.get('terms_title', '今日新词')}"]
        for t in r["terms"]:
            out.append(f"- **{t['term']}**：{t.get('explain', '')}")
    return "\n".join(out)


def to_short(r):
    lines = []
    for i in sorted([i for i in r["items"] if i["must"]], key=lambda i: i["must"]):
        lines.append(f"{i['must']}.【{i['source']}】{i['title_zh']}\n{i['url']}")
    return "\n\n".join(lines)
