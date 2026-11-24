#!/usr/bin/env python3
"""把 reports/ 里的报告生成阅读器网站（site/）和 RSS 订阅源（site/feeds/）。"""
import glob
import json
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from xml.sax.saxutils import escape

import markdown

BASE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(BASE, "site")
SITE_URL = os.environ.get("SITE_URL", "https://niukutzu.github.io/daily-fetch").rstrip("/")
CST = timezone(timedelta(hours=8))

# 代号, 名字, 图标(RSS 用), 头像字, 头像颜色, 运行时间, 报告名, 一句话介绍
BOTS = [
    ("tech", "全球科技", "💻", "科", "#2563eb", "每天 08:00", "全球科技每日精选", "Hacker News、GitHub、Hugging Face 等 16 个来源"),
    ("politics", "国际政治", "🌍", "政", "#0f766e", "每天 08:15", "国际政治每日精选", "智库、通讯社与亚洲媒体等 16 个来源"),
    ("finance", "全球金融", "📈", "金", "#b45309", "每天 08:30", "全球金融每日精选", "Polymarket、FRED、Bloomberg 等 11 个来源"),
    ("reddit", "Reddit", "👾", "R", "#c2410c", "每天 08:45", "Reddit 每日精选", "Reddit 热门版块精选"),
    ("music", "电音与DJ", "🎛️", "音", "#7c3aed", "每天 09:00", "电音与 DJ 每日精选", "Resident Advisor、Sound On Sound 等 10 个来源"),
    ("commerce", "海外商业与套利", "💰", "商", "#15803d", "每天 09:15", "海外商业与套利每日精选", "海外赚钱模式与真实案例"),
    ("chiangmai", "清迈活动", "🌴", "迈", "#db2777", "每周三 09:30", "清迈未来 7 天活动", "Steve 的活动清单、Time Out 清迈、新闻搜索"),
    ("events", "演出与艺文", "🎫", "演", "#9333ea", "每周四 09:30", "演出与艺文每周排期", "广州、深圳、上海的现场与展览"),
    ("releases", "音乐发行", "💿", "发", "#0369a1", "每周六 09:30", "每周六全球音乐发行盘点", "Beatport、Pitchfork、Bandcamp 等 10 个来源"),
]


def top_items(body):
    """取第一个 ## 小节里的列表，拆成 标签 / 标题 / 说明，给对话流用。"""
    items, inside = [], False
    for line in body.splitlines():
        if line.startswith("## "):
            if inside:
                break
            inside = True
            continue
        if not inside or not line.lstrip().startswith("- "):
            continue
        t = line.strip()[2:].strip()
        m = re.match(r"\*\*【(.+?)】\s*\[(.+?)\]\((\S+?)\)\s*\*\*\s*[：:]?\s*(.*)", t)
        if m:
            tag, title, url, text = m.groups()
        else:
            plain = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t).replace("**", "")
            mt = re.match(r"【(.+?)】\s*(.*)", plain)
            tag, rest = (mt.group(1), mt.group(2)) if mt else ("", plain)
            title, _, text = rest.partition("：")
            url = ""
        text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text).replace("**", "").strip()
        items.append({"tag": tag.strip()[:30], "title": title.strip()[:120], "text": text[:220], "url": url if url.startswith("http") else ""})
        if len(items) >= 5:
            break
    return items


def _clean_md(s):
    """去掉链接、加粗等 markdown 符号，压成一行。空则返回空串。"""
    if s is None:
        return ""
    if not isinstance(s, str):
        s = str(s)
    s = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = s.replace("**", "").replace("__", "").replace("`", "")
    s = re.sub(r"\*([^*\n]+)\*", r"\1", s)
    return re.sub(r"\s+", " ", s).strip()


def _search_row(rid, bot, date, i, t="", e="", x="", s="", c=""):
    """一条情报。空字段省略，减小体积。"""
    row = {"r": rid, "b": bot, "d": date, "i": i}
    for k, v in (("t", t), ("e", e), ("x", x), ("s", s), ("c", c)):
        v = _clean_md(v)
        if v:
            row[k] = v
    return row


_ITEM_RE = re.compile(r"\s*(?:[-*]|\d+\.)\s+(.*\S)")


def _list_tx(raw):
    """列表项：标题用加粗或链接文字，没有就用前 40 字；其余给简介。"""
    link = re.search(r"\[([^\]]+)\]\([^)]*\)", raw)
    bold = re.search(r"\*\*(.+?)\*\*|__(.+?)__", raw)
    if link:
        t, rest = link.group(1), raw[:link.start()] + raw[link.end():]
    elif bold:
        t, rest = (bold.group(1) or bold.group(2) or ""), raw[:bold.start()] + raw[bold.end():]
    else:
        cleaned = _clean_md(raw)
        return cleaned[:40], cleaned[40:].strip()
    t = _clean_md(t)
    x = re.sub(r"^[：:\s]+", "", _clean_md(rest))
    if not t:
        cleaned = _clean_md(raw)
        return cleaned[:40], cleaned[40:].strip()
    return t, x


def _body_items(body):
    """正文里每个列表项一条。缩进的续行并进上一条。"""
    items, buf = [], None
    for line in body.splitlines():
        m = _ITEM_RE.match(line)
        if m:
            if buf is not None:
                items.append(buf)
            buf = m.group(1).strip()
            continue
        if buf is None:
            continue
        if line[:1] in (" ", "\t") and line.strip() and not line.lstrip().startswith("#"):
            buf += " " + line.strip()
            continue
        items.append(buf)
        buf = None
    if buf is not None:
        items.append(buf)
    out = []
    for raw in items:
        t, x = _list_tx(raw)
        if t or x:
            out.append((t, x))
    return out


def _rows_from_rich(rid, bot, date, json_path):
    try:
        with open(json_path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return []
    rows = []
    for it in doc.get("items") or []:
        if not isinstance(it, dict):
            continue
        iid = it.get("id", 0)
        if iid is None or iid == "":
            iid = 0
        rows.append(_search_row(
            rid, bot, date, iid,
            it.get("title_zh"), it.get("title_en"), it.get("intro"),
            it.get("source"), it.get("category"),
        ))
    return rows


def _rows_from_body(rid, bot, date, body):
    return [_search_row(rid, bot, date, n, t, "", x) for n, (t, x) in enumerate(_body_items(body))]


def render(md_text):
    html = markdown.markdown(md_text, extensions=["tables", "sane_lists"])
    # 网站上存了开关钥匙，报告里的 HTML 一律去掉脚本类内容
    html = re.sub(r"<\s*(script|iframe|object|embed|style|form)\b.*?(</\s*\1\s*>|$)", "", html, flags=re.I | re.S)
    html = re.sub(r"<\s*/?\s*(script|iframe|object|embed|style|form|input|button|meta|link)\b[^>]*>", "", html, flags=re.I)
    html = re.sub(r"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", html, flags=re.I)
    html = re.sub(r"(href|src)\s*=\s*([\"'])\s*javascript:[^\"']*\2", r'\1="#"', html, flags=re.I)
    # 外链新窗口打开
    return re.sub(r'<a href="(https?://[^"]+)"', r'<a href="\1" target="_blank" rel="noopener"', html)


def main():
    if os.path.exists(SITE):
        shutil.rmtree(SITE)
    os.makedirs(os.path.join(SITE, "r"))
    os.makedirs(os.path.join(SITE, "feeds"))

    index = []
    search = []
    for bot, name, icon, *_ in BOTS:
        for path in sorted(glob.glob(os.path.join(BASE, "reports", bot, "*.md")), reverse=True):
            date = os.path.basename(path)[:-3]
            text = open(path, encoding="utf-8").read()
            lines = text.splitlines()
            title = lines[0].lstrip("# ").strip() if lines and lines[0].startswith("#") else f"{name} · {date}"
            body = "\n".join(lines[1:]) if lines and lines[0].startswith("#") else text
            plain = "\n".join(l for l in body.splitlines() if not l.lstrip().startswith("#"))
            plain = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", plain)
            plain = re.sub(r"[#*>|`_-]+", " ", plain)
            excerpt = re.sub(r"\s+", " ", plain).strip()[:90]
            rid = f"{bot}-{date}"
            html = render(body)
            with open(os.path.join(SITE, "r", f"{rid}.html"), "w", encoding="utf-8") as f:
                f.write(html)
            # 结构化报告（Grok 样式版面）：同名 .json
            rich = os.path.exists(path[:-3] + ".json")
            if rich:
                shutil.copy(path[:-3] + ".json", os.path.join(SITE, "r", f"{rid}.json"))
                search.extend(_rows_from_rich(rid, bot, date, path[:-3] + ".json"))
            else:
                search.extend(_rows_from_body(rid, bot, date, body))
            index.append({"id": rid, "bot": bot, "date": date, "title": title, "excerpt": excerpt, "html": html,
                          "items": top_items(body), "words": len(re.sub(r"\s", "", plain)), "rich": rich})

    index.sort(key=lambda x: (x["date"], x["id"]), reverse=True)
    bots = [{"id": b, "name": n, "icon": i, "ch": c, "color": col, "sched": sc, "title": t, "desc": d}
            for b, n, i, c, col, sc, t, d in BOTS]
    # 网站上改的名字和头像（仓库 Variables 里的 BOT_PROFILES）
    try:
        prof = json.loads(os.environ.get("BOT_PROFILES") or "{}")
    except ValueError:
        prof = {}
    for bt in bots:
        bt["def"] = {"name": bt["name"], "ch": bt["ch"], "color": bt["color"]}
        p = prof.get(bt["id"]) if isinstance(prof, dict) else None
        if not isinstance(p, dict):
            continue
        if str(p.get("name", "")).strip():
            bt["name"] = str(p["name"]).strip()[:20]
        if str(p.get("ch", "")).strip():
            bt["ch"] = str(p["ch"]).strip()[:2]
        if re.fullmatch(r"#[0-9a-fA-F]{6}", str(p.get("color", ""))):
            bt["color"] = p["color"]
        if p.get("shape") in ("c", "f5", "sq", "st4", "cl7", "t3", "f6", "d", "p", "hx", "sp8", "f9"):
            bt["shape"] = p["shape"]
        img = str(p.get("img", ""))
        if re.fullmatch(r"data:image/(jpeg|png|webp);base64,[A-Za-z0-9+/=]{20,12000}", img):
            bt["img"] = img
    data = {
        "bots": bots,
        "items": [{k: v for k, v in it.items() if k != "html"} for it in index],
        "built": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
    }
    with open(os.path.join(SITE, "data.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    search.sort(key=lambda x: x["d"], reverse=True)
    with open(os.path.join(SITE, "search.json"), "w", encoding="utf-8") as f:
        json.dump(search, f, ensure_ascii=False, separators=(",", ":"))

    def feed(fname, title, items):
        out = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>',
            f"<title>{escape(title)}</title><link>{SITE_URL}/</link><description>{escape(title)}</description><language>zh-CN</language>",
        ]
        for it in items[:40]:
            dt = datetime.strptime(it["date"], "%Y-%m-%d").replace(hour=8, tzinfo=CST)
            link = f"{SITE_URL}/#{it['id']}"
            out.append(
                f"<item><title>{escape(it['title'])}</title><link>{link}</link><guid isPermaLink=\"false\">{it['id']}</guid>"
                f"<pubDate>{format_datetime(dt)}</pubDate><description>{escape(it['excerpt'])}</description>"
                f"<content:encoded><![CDATA[{it['html'].replace(']]>', ']]&gt;')}]]></content:encoded></item>"
            )
        out.append("</channel></rss>")
        with open(os.path.join(SITE, "feeds", fname), "w", encoding="utf-8") as f:
            f.write("\n".join(out))

    feed("all.xml", "黎明简报 · 全部", index)
    for bot, name, *_ in BOTS:
        feed(f"{bot}.xml", f"黎明简报 · {name}", [x for x in index if x["bot"] == bot])

    shutil.copy(os.path.join(BASE, "web", "index.html"), os.path.join(SITE, "index.html"))
    open(os.path.join(SITE, ".nojekyll"), "w").close()
    print(f"[SITE] {len(index)} 份报告")


if __name__ == "__main__":
    main()
