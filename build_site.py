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
            index.append({"id": rid, "bot": bot, "date": date, "title": title, "excerpt": excerpt, "html": html,
                          "items": top_items(body), "words": len(re.sub(r"\s", "", plain))})

    index.sort(key=lambda x: (x["date"], x["id"]), reverse=True)
    data = {
        "bots": [{"id": b, "name": n, "icon": i, "ch": c, "color": col, "sched": sc, "title": t, "desc": d}
                 for b, n, i, c, col, sc, t, d in BOTS],
        "items": [{k: v for k, v in it.items() if k != "html"} for it in index],
        "built": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
    }
    with open(os.path.join(SITE, "data.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

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

    feed("all.xml", "每日情报 · 全部", index)
    for bot, name, *_ in BOTS:
        feed(f"{bot}.xml", f"每日情报 · {name}", [x for x in index if x["bot"] == bot])

    shutil.copy(os.path.join(BASE, "web", "index.html"), os.path.join(SITE, "index.html"))
    open(os.path.join(SITE, ".nojekyll"), "w").close()
    print(f"[SITE] {len(index)} 份报告")


if __name__ == "__main__":
    main()
