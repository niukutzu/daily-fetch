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

BOTS = [
    ("tech", "全球科技", "💻"),
    ("politics", "国际政治", "🌍"),
    ("finance", "全球金融", "📈"),
    ("reddit", "Reddit", "👾"),
    ("music", "电音与DJ", "🎛️"),
    ("commerce", "海外商业与套利", "💰"),
    ("events", "演出与艺文", "🎫"),
    ("releases", "音乐发行", "💿"),
]


def render(md_text):
    html = markdown.markdown(md_text, extensions=["tables", "sane_lists"])
    # 外链新窗口打开
    return re.sub(r'<a href="(https?://[^"]+)"', r'<a href="\1" target="_blank" rel="noopener"', html)


def main():
    if os.path.exists(SITE):
        shutil.rmtree(SITE)
    os.makedirs(os.path.join(SITE, "r"))
    os.makedirs(os.path.join(SITE, "feeds"))

    index = []
    for bot, name, icon in BOTS:
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
            index.append({"id": rid, "bot": bot, "date": date, "title": title, "excerpt": excerpt, "html": html})

    index.sort(key=lambda x: (x["date"], x["id"]), reverse=True)
    data = {
        "bots": [{"id": b, "name": n, "icon": i} for b, n, i in BOTS],
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
    for bot, name, _ in BOTS:
        feed(f"{bot}.xml", f"每日情报 · {name}", [x for x in index if x["bot"] == bot])

    shutil.copy(os.path.join(BASE, "web", "index.html"), os.path.join(SITE, "index.html"))
    open(os.path.join(SITE, ".nojekyll"), "w").close()
    print(f"[SITE] {len(index)} 份报告")


if __name__ == "__main__":
    main()
