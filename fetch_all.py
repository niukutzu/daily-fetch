#!/usr/bin/env python3
"""
Unified 0-Token Polite Pre-Fetcher for 6 Daily Digest Bots
Supports: --bot tech|finance|politics|music|commerce|reddit|all
"""

import argparse
import html
import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, quote_plus

from bs4 import BeautifulSoup
from curl_cffi import requests as creq
import feedparser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
STATE_DIR = os.path.join(BASE_DIR, "state")
os.makedirs(OUTPUT_DIR, exist_ok=True)

CST = timezone(timedelta(hours=8))

# 摘要长度：全球科技 Bot 运行时会调长（见 run_tech_bot）
SUMMARY_LEN = 150

# Polite per-domain rate limiter (1.5 ~ 3.5s random delay per domain)
_domain_locks = {}
_domain_last_req = {}
_global_lock = threading.Lock()


def _wait_polite(url: str, min_delay: float = 1.5, max_delay: float = 3.5):
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    with _global_lock:
        if domain not in _domain_locks:
            _domain_locks[domain] = threading.Lock()
            _domain_last_req[domain] = 0.0
        lock = _domain_locks[domain]

    with lock:
        now = time.time()
        elapsed = now - _domain_last_req.get(domain, 0.0)
        target_delay = random.uniform(min_delay, max_delay)
        if elapsed < target_delay:
            time.sleep(target_delay - elapsed)
        _domain_last_req[domain] = time.time()


def polite_get(url: str, referer: str = "https://www.google.com/", timeout: int = 15, extra_headers: dict = None):
    _wait_polite(url)
    headers = {
        "Accept": "application/rss+xml, application/xml, application/atom+xml, text/xml, text/html, application/json;q=0.9, */*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer,
        "Cache-Control": "no-cache",
    }
    if extra_headers:
        headers.update(extra_headers)
    return creq.get(url, impersonate="chrome", headers=headers, timeout=timeout)


def clean_text(raw: str, max_len: int = 150) -> str:
    if not raw:
        return ""
    text = html.unescape(str(raw))
    if "<" in text and ">" in text:
        soup = BeautifulSoup(text, "lxml")
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = soup.get_text(" ", strip=True)
    # Remove Reddit RSS boilerplate
    text = re.sub(r"submitted by\s+/u/[A-Za-z0-9_-]+\s*(\[link\])?\s*(\[comments\])?", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text


def format_date(entry) -> str:
    for key in ("published", "updated", "created"):
        val = getattr(entry, key, None) or (entry.get(key) if isinstance(entry, dict) else None)
        if val:
            return str(val)[:25]
    return datetime.now(CST).strftime("%Y-%m-%d")


def parse_rss_text(xml_text: str, source_name: str, category: str, limit: int = 6, is_gnews: bool = False):
    d = feedparser.parse(xml_text)
    items = []
    seen_titles = set()
    for e in d.entries:
        title = clean_text(getattr(e, "title", ""), max_len=260)
        if not title:
            continue
        if is_gnews and " - " in title:
            parts = title.rsplit(" - ", 1)
            if len(parts[0]) > 12:
                title = parts[0].strip()
        # Filter generic site-name-only titles in Google News
        if title.lower() in {source_name.lower(), "ground news", "afflift", "blackhatworld", "affiliatefix"}:
            continue
        norm_t = re.sub(r"[^a-z0-9]+", "", title.lower())[:45]
        if norm_t in seen_titles:
            continue
        seen_titles.add(norm_t)

        link = getattr(e, "link", "")
        raw_desc = getattr(e, "summary", "") or getattr(e, "description", "")
        if not raw_desc and hasattr(e, "content") and e.content:
            raw_desc = e.content[0].get("value", "")

        # Special handling for Techmeme: extract direct external URL from description
        if source_name == "Techmeme" and raw_desc:
            soup = BeautifulSoup(raw_desc, "lxml")
            ext_a = soup.find("a", href=True)
            if ext_a and "techmeme.com" not in ext_a["href"]:
                link = ext_a["href"]

        summary = clean_text(raw_desc, max_len=SUMMARY_LEN)
        if is_gnews and norm_t in re.sub(r"[^a-z0-9]+", "", summary.lower()):
            summary = f"Via Google News RSS index ({source_name})"

        items.append({
            "source": source_name,
            "category": category,
            "title": title,
            "url": link,
            "published": format_date(e),
            "summary": summary,
        })
        if len(items) >= limit:
            break
    return items


def fetch_gnews_fallback(query: str, source_name: str, category: str, limit: int = 5):
    q_enc = quote_plus(query)
    url = f"https://news.google.com/rss/search?q={q_enc}&hl=en-US&gl=US&ceid=US:en"
    r = polite_get(url, referer="https://news.google.com/")
    if r.status_code == 200:
        return parse_rss_text(r.text, source_name, category, limit=limit, is_gnews=True)
    return []


def fetch_rss_with_fallback(source_name: str, category: str, rss_urls: list, gnews_query: str = None, limit: int = 5):
    for u in rss_urls:
        try:
            r = polite_get(u)
            if r.status_code == 200:
                items = parse_rss_text(r.text, source_name, category, limit=limit, is_gnews=False)
                if items:
                    return items, "L1/L2_DIRECT_RSS", u
        except Exception:
            pass
    if gnews_query:
        try:
            items = fetch_gnews_fallback(gnews_query, source_name, category, limit=limit)
            if items:
                return items, "L3_GNEWS_RSS_FALLBACK", f"gnews:{gnews_query}"
        except Exception:
            pass
    return [], "FAILED", ""


# ==============================================================================
# 1. TECH BOT SOURCES (16 Sources)
# ==============================================================================
def fetch_tech_github():
    url = "https://github.com/trending"
    r = polite_get(url, referer="https://github.com/")
    soup = BeautifulSoup(r.text, "lxml")
    items = []
    for row in soup.select("article.Box-row")[:10]:
        h2 = row.select_one("h2 a")
        if not h2:
            continue
        repo = re.sub(r"\s+", "", h2.get_text(strip=True))
        href = "https://github.com/" + repo.lstrip("/")
        desc_el = row.select_one("p")
        desc = clean_text(desc_el.get_text(" ", strip=True) if desc_el else "", 300)
        lang_el = row.select_one("[itemprop='programmingLanguage']")
        lang = lang_el.get_text(strip=True) if lang_el else "Code"
        stars_today_el = row.select_one("span.d-inline-block.float-sm-right")
        stars_today = stars_today_el.get_text(" ", strip=True) if stars_today_el else ""
        total_el = row.select_one("a[href$='/stargazers']")
        total = total_el.get_text(strip=True) if total_el else ""
        m_today = re.search(r"([\d,]+)", stars_today)
        items.append({
            "rank": len(items) + 1,
            "heat": {"stars_today": int(m_today.group(1).replace(",", "")) if m_today else None,
                     "stars_total": total, "lang": lang},
            "source": "GitHub Trending",
            "category": "Open Source",
            "title": repo,
            "url": href,
            "published": datetime.now(CST).strftime("%Y-%m-%d"),
            "summary": clean_text(f"[{lang} | {stars_today} | 总 {total} stars] {desc}", 400),
        })
    return items, "L2_CURL_CFFI_HTML", url


def fetch_tech_hn():
    url = "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=10"
    r = polite_get(url, referer="https://news.ycombinator.com/")
    data = r.json()
    items = []
    for hit in data.get("hits", [])[:8]:
        oid = hit.get("objectID")
        link = hit.get("url") or f"https://news.ycombinator.com/item?id={oid}"
        pts = hit.get("points", 0)
        cmts = hit.get("num_comments", 0)
        items.append({
            "rank": len(items) + 1,
            "heat": {"hn_points": pts, "hn_comments": cmts},
            "discuss": f"https://news.ycombinator.com/item?id={oid}",
            "source": "Hacker News",
            "category": "Tech Community",
            "title": clean_text(hit.get("title", ""), 160),
            "url": link,
            "published": str(hit.get("created_at", ""))[:19],
            "summary": f"HN Frontpage: {pts} points, {cmts} comments (https://news.ycombinator.com/item?id={oid})",
        })
    return items, "L1_OFFICIAL_API", url


def fetch_tech_hf():
    items = []
    # 1. Daily Papers
    u1 = "https://huggingface.co/api/daily_papers"
    r1 = polite_get(u1, referer="https://huggingface.co/papers")
    if r1.status_code == 200:
        for p in r1.json()[:4]:
            paper = p.get("paper", {})
            pid = paper.get("id", "")
            title = clean_text(p.get("title") or paper.get("title", ""), 160)
            upvotes = paper.get("upvotes", 0)
            summary = clean_text(paper.get("summary", ""), 450)
            items.append({
                "source": "Hugging Face",
                "category": "AI Papers & Models",
                "title": f"[Paper] {title}",
                "url": f"https://huggingface.co/papers/{pid}",
                "published": str(p.get("publishedAt", ""))[:19],
                "heat": {"hf_upvotes": upvotes},
                "summary": clean_text(f"👍 {upvotes} upvotes | {summary}", 480),
            })
    # 2. Trending Models/Spaces
    u2 = "https://huggingface.co/api/trending"
    r2 = polite_get(u2, referer="https://huggingface.co/")
    if r2.status_code == 200:
        tr = r2.json().get("recentlyTrending", [])
        for entry in tr[:4]:
            rd = entry.get("repoData", {})
            rid = rd.get("id", "")
            rtype = entry.get("repoType", "model")
            likes = rd.get("likes", 0)
            downloads = rd.get("downloads", 0)
            prefix = "spaces/" if rtype == "space" else ("datasets/" if rtype == "dataset" else "")
            items.append({
                "source": "Hugging Face",
                "category": "AI Papers & Models",
                "title": f"[{rtype.capitalize()}] {rid}",
                "url": f"https://huggingface.co/{prefix}{rid}",
                "published": str(rd.get("lastModified", ""))[:19],
                "heat": {"hf_likes": likes, "hf_downloads": downloads},
                "summary": f"Trending {rtype}: ❤️ {likes} likes, ⬇️ {downloads} downloads",
            })
    return items, "L1_OFFICIAL_API", u1


def fetch_tech_artificial_analysis():
    url = "https://artificialanalysis.ai/leaderboards/models"
    r = polite_get(url, referer="https://artificialanalysis.ai/")
    soup = BeautifulSoup(r.text, "lxml")
    items = []
    for tr in soup.select("table tbody tr")[:6]:
        cols = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
        if len(cols) >= 6:
            model, ctx, creator, idx_score, price, speed = cols[:6]
            items.append({
                "source": "Artificial Analysis",
                "category": "AI Leaderboard",
                "title": f"{model} ({creator}) — Intelligence Index: {idx_score}",
                "url": url,
                "published": datetime.now(CST).strftime("%Y-%m-%d"),
                "summary": f"Context: {ctx} | Intelligence Index: {idx_score} | Blended Price: {price}/1M tokens | Speed: {speed} tok/s",
            })
    if items:
        return items, "L2_CURL_CFFI_HTML", url
    return fetch_gnews_fallback("site:artificialanalysis.ai", "Artificial Analysis", "AI Leaderboard", 4), "L3_GNEWS_RSS_FALLBACK", "gnews"


def fetch_tech_semianalysis():
    url = "https://newsletter.semianalysis.com/api/v1/archive?sort=new&limit=5"
    r = polite_get(url, referer="https://newsletter.semianalysis.com/")
    if r.status_code == 200:
        items = []
        data = r.json()
        posts = data.get("posts", []) if isinstance(data, dict) else data
        for post in posts[:5]:
            items.append({
                "source": "SemiAnalysis",
                "category": "Semiconductors & AI Infra",
                "title": clean_text(post.get("title", ""), 160),
                "url": post.get("canonical_url") or f"https://newsletter.semianalysis.com/p/{post.get('slug','')}",
                "published": str(post.get("post_date", ""))[:19],
                "summary": clean_text(post.get("subtitle") or post.get("description", ""), 400),
                "paywall": post.get("audience") == "only_paid",
            })
        if items:
            return items, "L1_SUBSTACK_API", url
    return fetch_rss_with_fallback("SemiAnalysis", "Semiconductors & AI Infra", ["https://newsletter.semianalysis.com/feed"], "site:semianalysis.com", 5)


def fetch_tech_the_batch():
    url = "https://www.deeplearning.ai/the-batch/"
    r = polite_get(url, referer="https://www.deeplearning.ai/")
    soup = BeautifulSoup(r.text, "lxml")
    items = []
    for art in soup.find_all("article")[:5]:
        links = [a["href"] for a in art.find_all("a", href=True) if "/the-batch/" in a["href"] and "/tag/" not in a["href"]]
        h = art.find(["h1", "h2", "h3", "h4"])
        p = art.find("p")
        if h and links:
            href = links[0]
            if href.startswith("/"):
                href = "https://www.deeplearning.ai" + href
            items.append({
                "source": "The Batch",
                "category": "AI Newsletter",
                "title": clean_text(h.get_text(" ", strip=True), 160),
                "url": href,
                "published": datetime.now(CST).strftime("%Y-%m-%d"),
                "summary": clean_text(p.get_text(" ", strip=True) if p else "DeepLearning.AI Weekly Issue by Andrew Ng", 400),
            })
    if items:
        return items, "L2_CURL_CFFI_HTML", url
    return fetch_gnews_fallback("site:deeplearning.ai/the-batch", "The Batch", "AI Newsletter", 4), "L3_GNEWS_RSS_FALLBACK", "gnews"


def fetch_tech_tldr():
    items = []
    for sub in ("tech", "ai"):
        url = f"https://tldr.tech/api/latest/{sub}"
        r = None
        for _attempt in range(2):
            try:
                r = polite_get(url, referer="https://tldr.tech/")
                if r.status_code == 200:
                    break
            except Exception:
                time.sleep(1.5)
        if r is None:
            continue
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "lxml")
            for a in soup.select("article a, div a"):
                h3 = a.find("h3")
                if not h3:
                    continue
                title = clean_text(h3.get_text(" ", strip=True), 160)
                if "(sponsor)" in title.lower():
                    continue
                href = a.get("href", "")
                parent = a.find_parent("article") or a.find_parent("div")
                full_txt = parent.get_text(" ", strip=True) if parent else ""
                desc = full_txt.replace(title, "").strip()
                items.append({
                    "source": "TLDR Tech / AI",
                    "category": f"TLDR {sub.upper()}",
                    "title": title,
                    "url": href,
                    "published": datetime.now(CST).strftime("%Y-%m-%d"),
                    "summary": clean_text(desc, 450),
                })
                if len([x for x in items if x["category"] == f"TLDR {sub.upper()}"]) >= 6:
                    break
    return items, "L1_OFFICIAL_API", "https://tldr.tech/api/latest/tech"


# ------------------------------------------------------------------------------
# 全球科技加料：真实信源、热度文字、原文开头一段、GitHub README 简介
# ------------------------------------------------------------------------------
PUBLISHERS = {
    "cnbc.com": "CNBC", "bloomberg.com": "Bloomberg", "wsj.com": "Wall Street Journal", "nytimes.com": "New York Times",
    "ft.com": "Financial Times", "axios.com": "Axios", "wired.com": "Wired", "theverge.com": "The Verge",
    "techcrunch.com": "TechCrunch", "zdnet.com": "ZDNET", "reuters.com": "Reuters", "semafor.com": "Semafor",
    "electrek.co": "Electrek", "arstechnica.com": "Ars Technica", "technologyreview.com": "MIT Tech Review",
    "github.com": "GitHub", "huggingface.co": "Hugging Face", "twitter.com": "X", "x.com": "X",
    "openai.com": "OpenAI", "anthropic.com": "Anthropic", "quantamagazine.org": "Quanta Magazine",
    "theinformation.com": "The Information", "9to5mac.com": "9to5Mac", "9to5google.com": "9to5Google",
    "macrumors.com": "MacRumors", "engadget.com": "Engadget", "businessinsider.com": "Business Insider",
    "cnn.com": "CNN", "bbc.com": "BBC", "bbc.co.uk": "BBC", "theguardian.com": "The Guardian",
    "washingtonpost.com": "Washington Post", "simonwillison.net": "Simon Willison", "latent.space": "Latent Space",
    "deeplearning.ai": "The Batch", "producthunt.com": "Product Hunt", "yankodesign.com": "Yanko Design",
    "metr.org": "METR", "z.ai": "Z.ai", "thinkingmachines.ai": "Thinking Machines", "blog.google": "Google",
    "googleblog.com": "Google", "ai.meta.com": "Meta", "about.fb.com": "Meta", "microsoft.com": "Microsoft",
    "apple.com": "Apple", "nvidia.com": "Nvidia", "nvidianews.nvidia.com": "Nvidia", "tomshardware.com": "Tom's Hardware",
    "theregister.com": "The Register", "politico.eu": "Politico", "politico.com": "Politico", "404media.co": "404 Media",
    "platformer.news": "Platformer", "stratechery.com": "Stratechery", "semianalysis.com": "SemiAnalysis",
    "importai.substack.com": "Import AI", "artificialanalysis.ai": "Artificial Analysis", "youtube.com": "YouTube",
    "economist.com": "The Economist", "fortune.com": "Fortune", "forbes.com": "Forbes", "venturebeat.com": "VentureBeat",
}
# 需要付费订阅才能看全文的网站（硬付费墙）。这些链接在报告里标 🔒，同一件事有免费报道时优先用免费的。
PAYWALL = ("bloomberg.com", "wsj.com", "ft.com", "nytimes.com", "theinformation.com", "economist.com", "barrons.com",
           "washingtonpost.com", "nikkei.com", "foreignaffairs.com", "theatlantic.com", "businessinsider.com",
           "newyorker.com", "stratechery.com", "theathletic.com", "fortune.com", "seekingalpha.com", "puck.news")
# 免费、可靠的新闻媒体（Techmeme 同一事件的其他报道里按这个顺序挑）
FREE_OK = ("apnews.com", "reuters.com", "bbc.com", "bbc.co.uk", "theguardian.com", "npr.org", "cnbc.com", "axios.com",
           "theverge.com", "techcrunch.com", "arstechnica.com", "semafor.com", "zdnet.com", "theregister.com",
           "engadget.com", "cnn.com", "nbcnews.com", "cbsnews.com", "abcnews.go.com", "aljazeera.com", "dw.com",
           "venturebeat.com", "geekwire.com", "9to5mac.com", "9to5google.com", "macrumors.com", "tomshardware.com",
           "cnet.com", "techradar.com", "politico.com", "politico.eu", "404media.co", "electrek.co")


def _dom_in(url: str, doms) -> bool:
    h = _host(url)
    return bool(h) and any(h == d or h.endswith("." + d) for d in doms)


def is_paywalled(url: str) -> bool:
    # 分享 / 赠阅链接（纽约时报 unlocked_article_code、华尔街日报 st=、FT accessToken）任何人都能免费看
    if re.search(r"unlocked_article_code=|[?&]st=|accessToken=|[?&]gift=|giftCopy", url or ""):
        return False
    return _dom_in(url, PAYWALL)


def techmeme_free_alternatives() -> dict:
    """Techmeme 首页：每条头条下面列了其他媒体的同题报道。返回 {头条链接: [(媒体名, 免费链接), ...]}"""
    try:
        r = polite_get("https://www.techmeme.com/", referer="https://www.techmeme.com/")
        soup = BeautifulSoup(r.text, "lxml")
    except Exception:
        return {}
    alts, cur = {}, None
    for clus in soup.select("div.clus"):
        cur = None
        for a in clus.find_all("a", href=True):
            href = a["href"]
            if "ourh" in (a.get("class") or []):
                cur = href
                alts.setdefault(cur, [])
                continue
            name = a.get_text(strip=True)
            if cur and name and not name.startswith("@") and urlparse(href).path not in ("", "/") and _dom_in(href, FREE_OK):
                alts[cur].append((name, href))
    for k, v in alts.items():  # 按 FREE_OK 的顺序排（越靠前越优先）
        v.sort(key=lambda x: next((i for i, d in enumerate(FREE_OK) if _dom_in(x[1], (d,))), 99))
    return alts


NO_LEAD = ("github.com", "huggingface.co", "artificialanalysis.ai", "news.google.com", "twitter.com", "x.com", "youtube.com")


def _host(url: str) -> str:
    h = urlparse(url or "").netloc.lower()
    return h[4:] if h.startswith("www.") else h


def publisher_of(url: str, fallback: str) -> str:
    h = _host(url)
    if not h:
        return fallback
    for dom, name in PUBLISHERS.items():
        if h == dom or h.endswith("." + dom):
            return name
    parts = h.split(".")
    label = parts[-2] if len(parts) >= 2 else parts[0]
    if label in ("co", "com", "org", "net") and len(parts) >= 3:
        label = parts[-3]
    return label[:1].upper() + label[1:]


def strip_tracking(url: str) -> str:
    if "utm_" not in url and "reflink=" not in url:
        return url
    base, _, rest = url.partition("?")
    frag = ""
    if "#" in rest:
        rest, frag = rest.split("#", 1)
        frag = "#" + frag
    keep = [kv for kv in rest.split("&") if kv and not re.match(r"(utm_\w+|reflink)=", kv)]
    return base + ("?" + "&".join(keep) if keep else "") + frag


def fetch_lead(url: str, max_len: int = 700) -> str:
    """抓原文：取 og:description + 正文前两段（拿不到就返回空）。"""
    try:
        r = polite_get(url, timeout=12)
        if r.status_code != 200 or "html" not in r.headers.get("content-type", "html"):
            return ""
        soup = BeautifulSoup(r.text[:600000], "lxml")
        parts = []
        for sel in ("meta[property='og:description']", "meta[name='description']", "meta[name='twitter:description']"):
            m = soup.select_one(sel)
            if m and m.get("content"):
                parts.append(clean_text(m["content"], 400))
                break
        root = soup.find("article") or soup.find("main") or soup.body or soup
        n = 0
        for p in root.find_all("p"):
            t = clean_text(p.get_text(" ", strip=True), 450)
            if len(t) < 70 or any(t[:60] in x for x in parts) or re.search(r"cookie|subscribe|sign up|newsletter|javascript", t, re.I):
                continue
            parts.append(t)
            n += 1
            if n >= 2:
                break
        return clean_text(" ".join(parts), max_len)
    except Exception:
        return ""


def fetch_readme_intro(repo: str, max_len: int = 600) -> str:
    """GitHub 项目 README 的开头几段文字（去掉徽章、图片、代码）。"""
    for branch in ("HEAD",):
        try:
            r = polite_get(f"https://raw.githubusercontent.com/{repo}/{branch}/README.md", referer="https://github.com/", timeout=12)
            if r.status_code != 200:
                continue
            txt = re.sub(r"```.*?```", " ", r.text[:40000], flags=re.S)
            txt = re.sub(r"<[^>]+>", " ", txt)
            txt = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", txt)
            txt = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", txt)
            keep = []
            for line in txt.splitlines():
                t = line.strip().strip("#>*-|_ ").strip()
                if len(t) < 30 or t.count("|") > 2 or re.match(r"^(table of contents|contents|license|install)", t, re.I):
                    continue
                keep.append(t)
                if sum(len(k) for k in keep) > max_len:
                    break
            return clean_text(" ".join(keep), max_len)
        except Exception:
            pass
    return ""


def heat_text(it: dict) -> str:
    h = it.get("heat") or {}
    if "hn_points" in h:
        return f"HN {h['hn_points']} 分 / {h['hn_comments']} 评"
    if h.get("stars_today") is not None:
        return f"今日 +{h['stars_today']} stars（总 {h.get('stars_total') or '?'}）"
    if "hf_likes" in h:
        return f"HF {h['hf_likes']} 喜欢 / {h['hf_downloads']} 下载"
    if "hf_upvotes" in h:
        return f"HF 论文 {h['hf_upvotes']} 赞"
    if it.get("source") == "Techmeme":
        return "Techmeme 头条"
    return ""


def enrich_tech(items: list):
    t0 = time.time()
    for it in items:
        it["url"] = strip_tracking(it.get("url", ""))
        if it["source"] == "Techmeme":
            m = re.search(r"\s*\(([^()]{2,60})\)\s*$", it["title"])
            if m:
                it["publisher"] = m.group(1).split("/")[-1].strip()
                it["title"] = it["title"][: m.start()].strip()
            it["summary"] = re.sub(r"^[^:]{2,60}\s:\s", "", it["summary"])
        if it["source"].startswith("TLDR"):
            it["title"] = re.sub(r"\s*\(\d+ minute read\)\s*$", "", it["title"])
        it.setdefault("publisher", publisher_of(it["url"], it["source"]) if it["source"] in ("Hacker News", "Techmeme", "TLDR Tech / AI") else it["source"])
        it["heat_text"] = heat_text(it)

    # Techmeme 头条是付费媒体的：换成同一事件的免费报道（原付费链接留作备注）
    alts = techmeme_free_alternatives()
    swapped = 0
    for it in items:
        if it["source"] == "Techmeme" and is_paywalled(it["url"]):
            key = it["url"]
            cand = alts.get(key) or next((v for k, v in alts.items() if k.split("?")[0] == key.split("?")[0]), [])
            if cand:
                url = cand[0][1]
                it["paywall_url"], it["url"], it["publisher"] = it["url"], strip_tracking(url), publisher_of(url, it["source"])
                swapped += 1
    if swapped:
        print(f"[TECH] Techmeme 付费链接换成免费报道：{swapped} 条")

    # 同一链接在 HN 也出现：给其他条目补上 HN 热度
    hn = {re.sub(r"[?#].*$", "", x["url"]).rstrip("/"): x for x in items if x["source"] == "Hacker News"}
    for it in items:
        k = re.sub(r"[?#].*$", "", it["url"]).rstrip("/")
        if it["source"] != "Hacker News" and k in hn:
            it["heat_text"] = (it["heat_text"] + "；" if it["heat_text"] else "") + hn[k]["heat_text"]

    jobs = []
    for it in items:
        if it["source"] == "GitHub Trending":
            jobs.append((it, lambda u=it["title"]: fetch_readme_intro(u)))
        elif _host(it["url"]) and not any(_host(it["url"]).endswith(d) for d in NO_LEAD) and (
                it["source"] in ("Hacker News", "Techmeme", "TLDR Tech / AI", "Product Hunt") or len(it.get("summary", "")) < 120):
            jobs.append((it, lambda u=it["url"]: fetch_lead(u)))
    jobs = jobs[:45]
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(fn): it for it, fn in jobs}
        for f in as_completed(futs):
            try:
                lead = f.result()
            except Exception:
                lead = ""
            if lead:
                futs[f]["lead"] = lead
    got = sum(1 for it, _ in jobs if it.get("lead"))
    print(f"[TECH] 原文开头：{got}/{len(jobs)} 条拿到，用时 {round(time.time() - t0, 1)}s")


def enrich_generic(items: list, lead_limit: int = 25):
    """非科技 Bot 的加料：去跟踪参数、热度文字、Reddit 热帖排名、短摘要条目抓原文开头。"""
    t0 = time.time()
    for it in items:
        it["url"] = strip_tracking(it.get("url", ""))
        summ = it.get("summary", "")
        m = re.match(r"score=(\d+) comments=(\d+)", summ)
        if m:
            it["heat_text"] = f"Reddit {m.group(1)} 赞 / {m.group(2)} 评"
            it["_score"] = int(m.group(1))
        m = re.match(r"24h Vol: \$([\d,]+)", summ)
        if m:
            it["heat_text"] = f"24 小时成交 ${int(m.group(1).replace(',', '')) / 1e6:.2f}M"
        m = re.search(r"(\d+) pts, (\d+) comments", summ)
        if m:
            it["heat_text"] = f"HN {m.group(1)} 分 / {m.group(2)} 评"
    pm = [it for it in items if it["source"] == "Polymarket"]
    for k, it in enumerate(pm, 1):
        it["rank"] = k
    reddit = sorted([it for it in items if "_score" in it], key=lambda x: -x["_score"])
    for k, it in enumerate(reddit[:10], 1):
        it["rank"], it["rank_group"] = k, "Reddit"
    for it in items:
        it.pop("_score", None)

    jobs = [it for it in items if len(it.get("summary", "")) < 160 and _host(it.get("url", ""))
            and not any(_host(it["url"]).endswith(d) for d in NO_LEAD + ("reddit.com",))][:lead_limit]
    if not jobs:
        return
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(fetch_lead, it["url"]): it for it in jobs}
        for f in as_completed(futs):
            try:
                lead = f.result()
            except Exception:
                lead = ""
            if lead:
                futs[f]["lead"] = lead
    print(f"[加料] 原文开头：{sum(1 for it in jobs if it.get('lead'))}/{len(jobs)} 条拿到，用时 {round(time.time() - t0, 1)}s")


def run_tech_bot():
    tasks = [
        ("GitHub Trending", fetch_tech_github),
        ("Hacker News", fetch_tech_hn),
        ("Techmeme", lambda: fetch_rss_with_fallback("Techmeme", "Tech News", ["https://www.techmeme.com/feed.xml"], "site:techmeme.com", 15)),
        ("Hugging Face", fetch_tech_hf),
        ("Artificial Analysis", fetch_tech_artificial_analysis),
        ("Ars Technica", lambda: fetch_rss_with_fallback("Ars Technica", "Tech Journalism", ["https://feeds.arstechnica.com/arstechnica/index"], "site:arstechnica.com", 5)),
        ("MIT Tech Review", lambda: fetch_rss_with_fallback("MIT Tech Review", "Deep Tech", ["https://www.technologyreview.com/feed/"], "site:technologyreview.com", 5)),
        ("SemiAnalysis", fetch_tech_semianalysis),
        ("The Verge", lambda: fetch_rss_with_fallback("The Verge", "Tech News", ["https://www.theverge.com/rss/index.xml"], "site:theverge.com", 6)),
        ("Simon Willison", lambda: fetch_rss_with_fallback("Simon Willison", "AI Engineering", ["https://simonwillison.net/atom/everything/"], "site:simonwillison.net", 5)),
        ("Latent Space", lambda: fetch_rss_with_fallback("Latent Space", "AI Engineering", ["https://www.latent.space/feed"], "site:latent.space", 4)),
        ("The Batch", fetch_tech_the_batch),
        ("TLDR Tech / AI", fetch_tech_tldr),
        ("Import AI", lambda: fetch_rss_with_fallback("Import AI", "AI Policy & Research", ["https://importai.substack.com/feed"], "site:importai.substack.com", 4)),
        ("Product Hunt", lambda: fetch_rss_with_fallback("Product Hunt", "New Products", ["https://www.producthunt.com/feed"], "site:producthunt.com", 5)),
        ("Yanko Design", lambda: fetch_rss_with_fallback("Yanko Design", "Industrial Design", ["https://www.yankodesign.com/feed/"], "site:yankodesign.com", 4)),
    ]
    global SUMMARY_LEN
    old_len, SUMMARY_LEN = SUMMARY_LEN, 450
    try:
        return execute_bot_tasks("tech", tasks, post=enrich_tech)
    finally:
        SUMMARY_LEN = old_len


# ==============================================================================
# 2. FINANCE BOT SOURCES (11 Sources)
# ==============================================================================
def fetch_finance_polymarket():
    url = "https://gamma-api.polymarket.com/events?active=true&closed=false&order=volume24hr&ascending=false&limit=10"
    r = polite_get(url, referer="https://polymarket.com/")
    data = r.json()
    items = []
    for ev in data[:8]:
        title = clean_text(ev.get("title", ""), 150)
        slug = ev.get("slug", "")
        vol24 = float(ev.get("volume24hr") or 0)
        vol_total = float(ev.get("volume") or 0)
        markets = ev.get("markets", [])
        odds = []
        for m in markets:
            q = m.get("groupItemTitle") or m.get("question") or ""
            prices = m.get("outcomePrices")
            if isinstance(prices, str):
                try:
                    prices = json.loads(prices)
                except Exception:
                    prices = []
            if prices and len(prices) > 0:
                try:
                    if not m.get("closed"):
                        odds.append((float(prices[0]), q))
                except Exception:
                    pass
        odds.sort(key=lambda x: -x[0])  # 按概率从高到低，只留前 5 个选项
        odds_str = " | ".join(f"{q}: {round(p * 100, 1)}%" for p, q in odds[:5])
        items.append({
            "source": "Polymarket",
            "category": "Prediction Market",
            "title": title,
            "url": f"https://polymarket.com/event/{slug}",
            "published": datetime.now(CST).strftime("%Y-%m-%d"),
            "summary": clean_text(f"24h Vol: ${vol24:,.0f} (Total: ${vol_total:,.0f}) | {odds_str}", 450),
        })
    return items, "L1_OFFICIAL_API", url


def fetch_finance_fred_and_macro():
    items = []
    # 1. FRED Blog RSS
    rss_items, _, _ = fetch_rss_with_fallback("FRED", "Macro Data", ["https://fredblog.stlouisfed.org/feed/"], "site:fred.stlouisfed.org", 3)
    items.extend(rss_items)
    # 2. FRED Direct Series CSV (MORTGAGE30US, DGS10)
    for sid, label in [("MORTGAGE30US", "30-Year Fixed Rate Mortgage Average"), ("DGS10", "10-Year Treasury Constant Maturity Rate")]:
        try:
            u = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"
            r = polite_get(u, referer="https://fred.stlouisfed.org/")
            if r.status_code == 200:
                lines = [ln.strip() for ln in r.text.strip().splitlines() if ln.strip() and not ln.endswith(",.")]
                if len(lines) >= 2:
                    dt, val = lines[-1].split(",")[:2]
                    prev_dt, prev_val = lines[-2].split(",")[:2]
                    items.append({
                        "source": "FRED",
                        "category": "Macro Official Series",
                        "title": f"{label} ({sid}): {val}% (as of {dt})",
                        "url": f"https://fred.stlouisfed.org/series/{sid}",
                        "published": dt,
                        "summary": f"Official St. Louis Fed FRED series {sid}: latest {val}% on {dt} (previous: {prev_val}% on {prev_dt})",
                    })
        except Exception:
            pass
    # 3. Yahoo Finance Live Macro Tickers
    tickers = [("%5EGSPC", "S&P 500"), ("%5EIXIC", "Nasdaq"), ("%5ETNX", "US 10Y Yield"), ("DX-Y.NYB", "US Dollar Index"), ("GC%3DF", "Gold Futures"), ("CL%3DF", "WTI Crude Oil"), ("BTC-USD", "Bitcoin")]
    macro_parts = []
    for sym, name in tickers:
        try:
            u = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=2d"
            r = polite_get(u, referer="https://finance.yahoo.com/")
            if r.status_code == 200:
                meta = r.json()["chart"]["result"][0]["meta"]
                price = meta.get("regularMarketPrice")
                prev = meta.get("chartPreviousClose") or meta.get("previousClose")
                if price and prev:
                    chg = ((price - prev) / prev) * 100
                    macro_parts.append(f"{name}: {price:,.2f} ({chg:+.2f}%)")
        except Exception:
            pass
    if macro_parts:
        items.append({
            "source": "FRED",
            "category": "Global Macro Snapshot",
            "title": "Live Global Macro Tickers (S&P 500, Nasdaq, 10Y Yield, DXY, Gold, WTI, BTC)",
            "url": "https://finance.yahoo.com/markets/",
            "published": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "summary": clean_text(" | ".join(macro_parts), 600),
        })
    return items, "L1_OFFICIAL_API_AND_RSS", "https://fredblog.stlouisfed.org/feed/"


def fetch_finance_nick_timiraos():
    items = []
    # Direct WSJ Economy RSS
    wsj_items, _, _ = fetch_rss_with_fallback("Nick Timiraos (WSJ)", "Fed & US Economy", ["https://feeds.content.dowjones.io/public/rss/WSJcomUSBusiness"], None, 4)
    # Google News RSS specifically for Nick Timiraos
    nt_items = fetch_gnews_fallback('site:wsj.com "Nick Timiraos"', "Nick Timiraos (WSJ)", "Fed Watch", limit=3)
    items.extend(nt_items)
    items.extend(wsj_items[:3])
    return items[:5], "L1_RSS_PLUS_L3_GNEWS", "https://feeds.content.dowjones.io/public/rss/WSJcomUSBusiness"


def fetch_finance_money_stuff():
    items = []
    ms_items = fetch_gnews_fallback('site:bloomberg.com "Matt Levine" OR "Money Stuff"', "Money Stuff (Bloomberg)", "Wall Street Column", limit=3)
    bb_items, _, _ = fetch_rss_with_fallback("Money Stuff (Bloomberg)", "Bloomberg Markets", ["https://feeds.bloomberg.com/markets/news.rss"], None, 3)
    items.extend(ms_items)
    items.extend(bb_items[:3])
    return items[:5], "L1_RSS_PLUS_L3_GNEWS", "https://feeds.bloomberg.com/markets/news.rss"


def fetch_finance_apollo():
    items = []
    # 1. Apollo Academy WP REST API
    u = "https://www.apolloacademy.com/wp-json/wp/v2/posts?per_page=4"
    try:
        r = polite_get(u, referer="https://www.apolloacademy.com/")
        if r.status_code == 200:
            for p in r.json()[:3]:
                items.append({
                    "source": "Apollo Daily Spark",
                    "category": "Macro Charts",
                    "title": clean_text(p.get("title", {}).get("rendered", ""), 160),
                    "url": p.get("link", ""),
                    "published": str(p.get("date", ""))[:19],
                    "summary": clean_text(p.get("excerpt", {}).get("rendered", ""), 150),
                })
    except Exception:
        pass
    # 2. Supplement with Google News for Torsten Slok / Daily Spark
    gn = fetch_gnews_fallback('site:apolloacademy.com OR site:apollo.com "Torsten Slok" OR "Daily Spark"', "Apollo Daily Spark", "Macro Charts", limit=3)
    items.extend(gn)
    return items[:5], "L1_WP_API_PLUS_L3_GNEWS", u


def run_finance_bot():
    tasks = [
        ("Polymarket", fetch_finance_polymarket),
        ("Visual Capitalist", lambda: fetch_rss_with_fallback("Visual Capitalist", "Data Visualization", ["https://www.visualcapitalist.com/feed/"], "site:visualcapitalist.com", 5)),
        ("美联储官方公告", lambda: fetch_rss_with_fallback("Federal Reserve", "Fed Official", ["https://www.federalreserve.gov/feeds/press_all.xml"], None, 5)),
        ("AP 商业新闻", lambda: fetch_rss_with_fallback("AP News", "Business", [], "site:apnews.com business OR economy OR markets", 6)),
        ("FRED", fetch_finance_fred_and_macro),
        ("Morning Brew", lambda: fetch_rss_with_fallback("Morning Brew", "Business Daily", [], "site:morningbrew.com/stories", 5)),
        ("Yahoo Finance", lambda: fetch_rss_with_fallback("Yahoo Finance", "Markets", ["https://feeds.finance.yahoo.com/rss/2.0/headline?s=%5EGSPC,%5EIXIC,BTC-USD,NVDA,AAPL,MSFT,TSLA&region=US&lang=en-US"], "site:finance.yahoo.com/news", 5)),
        ("Reuters Markets", lambda: fetch_rss_with_fallback("Reuters Markets", "Global Markets", [], "site:reuters.com/markets when:2d", 5)),
        ("CNBC 经济", lambda: fetch_rss_with_fallback("CNBC", "Economy & Markets", ["https://www.cnbc.com/id/20910258/device/rss/rss.html"], "site:cnbc.com economy OR markets OR fed", 6)),
        ("Apollo Daily Spark", fetch_finance_apollo),
        ("Sherwood News", lambda: fetch_rss_with_fallback("Sherwood News", "Markets & Tech", ["https://sherwood.news/rss.xml"], "site:sherwood.news", 5)),
    ]
    return execute_bot_tasks("finance", tasks, post=enrich_generic)


# ==============================================================================
# 3. POLITICS BOT SOURCES (16 Sources)
# ==============================================================================
def fetch_politics_carnegie():
    url = "https://carnegieendowment.org/"
    try:
        r = polite_get(url)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "lxml")
            items = []
            seen = set()
            for a in soup.find_all("a", href=True):
                href = a["href"]
                txt = clean_text(a.get_text(" ", strip=True), 160)
                if ("/research/2026/" in href or "/emissary/2026/" in href) and len(txt) > 20 and href not in seen:
                    seen.add(href)
                    full_url = "https://carnegieendowment.org" + href if href.startswith("/") else href
                    items.append({
                        "source": "Carnegie Endowment",
                        "category": "Think Tank",
                        "title": txt,
                        "url": full_url,
                        "published": datetime.now(CST).strftime("%Y-%m-%d"),
                        "summary": f"Carnegie Endowment research publication ({href})",
                    })
                    if len(items) >= 5:
                        break
            if items:
                return items, "L2_CURL_CFFI_HTML", url
    except Exception:
        pass
    return fetch_gnews_fallback("site:carnegieendowment.org", "Carnegie Endowment", "Think Tank", 5), "L3_GNEWS_RSS_FALLBACK", "gnews"


def fetch_politics_chatham():
    url = "https://www.chathamhouse.org/"
    try:
        r = polite_get(url)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "lxml")
            items = []
            seen = set()
            for a in soup.find_all("a", href=True):
                href = a["href"]
                txt = clean_text(a.get_text(" ", strip=True), 160)
                if "/2026/" in href and len(txt) > 20 and href not in seen:
                    seen.add(href)
                    full_url = "https://www.chathamhouse.org" + href if href.startswith("/") else href
                    items.append({
                        "source": "Chatham House",
                        "category": "Think Tank",
                        "title": txt,
                        "url": full_url,
                        "published": datetime.now(CST).strftime("%Y-%m-%d"),
                        "summary": f"Chatham House analysis ({href})",
                    })
                    if len(items) >= 5:
                        break
            if items:
                return items, "L2_CURL_CFFI_HTML", url
    except Exception:
        pass
    return fetch_gnews_fallback("site:chathamhouse.org", "Chatham House", "Think Tank", 5), "L3_GNEWS_RSS_FALLBACK", "gnews"


def fetch_politics_ground_news():
    url = "https://ground.news/"
    try:
        r = polite_get(url)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "lxml")
            items = []
            seen = set()
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if href.startswith("/article/") and href not in seen:
                    h_el = a.find(["h2", "h3", "h4", "span"])
                    txt = clean_text(h_el.get_text(" ", strip=True) if h_el else a.get_text(" ", strip=True), 160)
                    txt = re.sub(r"^Info Icon\s+.*?(Reuters|Getty Images|AP)\s+", "", txt)
                    if len(txt) > 20:
                        seen.add(href)
                        items.append({
                            "source": "Ground News",
                            "category": "Media Bias & Aggregator",
                            "title": txt,
                            "url": "https://ground.news" + href,
                            "published": datetime.now(CST).strftime("%Y-%m-%d"),
                            "summary": clean_text(a.get_text(" ", strip=True), 150),
                        })
                        if len(items) >= 5:
                            break
            if items:
                return items, "L2_CURL_CFFI_HTML", url
    except Exception:
        pass
    return fetch_gnews_fallback("site:ground.news/article", "Ground News", "Media Bias & Aggregator", 5), "L3_GNEWS_RSS_FALLBACK", "gnews"


def run_politics_bot():
    tasks = [
        ("Reuters World", lambda: fetch_rss_with_fallback("Reuters World", "Wire Agency", [], "site:reuters.com/world when:2d", 6)),
        ("AP News World", lambda: fetch_rss_with_fallback("AP News World", "Wire Agency", [], "site:apnews.com/article (diplomacy OR military OR election OR government OR summit) when:2d", 5)),
        ("AFP", lambda: fetch_rss_with_fallback("AFP", "Wire Agency", [], '"AFP" world news OR diplomacy when:2d', 5)),
        ("BBC World", lambda: fetch_rss_with_fallback("BBC World", "World News", ["https://feeds.bbci.co.uk/news/world/rss.xml"], "site:bbc.com/news/world", 6)),
        ("Al Jazeera", lambda: fetch_rss_with_fallback("Al Jazeera", "World News", ["https://www.aljazeera.com/xml/rss/all.xml"], "site:aljazeera.com", 5)),
        ("ISW", lambda: fetch_rss_with_fallback("ISW", "Military & Conflict", [], "site:understandingwar.org", 4)),
        ("SIPRI", lambda: fetch_rss_with_fallback("SIPRI", "Arms & Security", ["https://www.sipri.org/rss/combined.xml"], "site:sipri.org", 4)),
        ("ICG (Crisis Group)", lambda: fetch_rss_with_fallback("ICG (Crisis Group)", "Conflict Watch", ["https://www.crisisgroup.org/rss.xml"], "site:crisisgroup.org", 4)),
        ("CSIS", lambda: fetch_rss_with_fallback("CSIS", "Think Tank", ["https://www.csis.org/rss.xml"], "site:csis.org", 5)),
        ("Carnegie Endowment", fetch_politics_carnegie),
        ("Chatham House", fetch_politics_chatham),
        ("Bruegel", lambda: fetch_rss_with_fallback("Bruegel", "EU Policy", ["https://www.bruegel.org/rss.xml"], "site:bruegel.org", 4)),
        ("CFR", lambda: fetch_rss_with_fallback("CFR", "Foreign Relations", ["https://feeds.cfr.org/cfr_main"], "site:cfr.org", 5)),
        ("Ground News", fetch_politics_ground_news),
        ("AllSides", lambda: fetch_rss_with_fallback("AllSides", "Cross-Partisan", ["https://www.allsides.com/rss/news"], "site:allsides.com", 4)),
        ("BBC Asia", lambda: fetch_rss_with_fallback("BBC Asia", "Asia Geopolitics", ["https://feeds.bbci.co.uk/news/world/asia/rss.xml"], "site:bbc.com/news/world/asia", 6)),
        ("SCMP", lambda: fetch_rss_with_fallback("SCMP", "China & Diplomacy", ["https://www.scmp.com/rss/91/feed"], "site:scmp.com", 6)),
    ]
    return execute_bot_tasks("politics", tasks, post=enrich_generic)


# ==============================================================================
# 4. MUSIC & DJ BOT SOURCES (10 Sources)
# ==============================================================================
def fetch_music_ra():
    items = []
    # 1. Direct HTML from ra.co/features
    url = "https://ra.co/features"
    try:
        r = polite_get(url, referer="https://ra.co/")
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "lxml")
            seen = set()
            for a in soup.find_all("a", href=True):
                href = a["href"]
                txt = clean_text(a.get_text(" ", strip=True), 160)
                if "/features/" in href and len(txt) > 18 and href not in seen:
                    seen.add(href)
                    full_url = "https://ra.co" + href if href.startswith("/") else href
                    items.append({
                        "source": "Resident Advisor",
                        "category": "Electronic Music & DJ",
                        "title": txt,
                        "url": full_url,
                        "published": datetime.now(CST).strftime("%Y-%m-%d"),
                        "summary": f"Resident Advisor feature ({full_url})",
                    })
                    if len(items) >= 3:
                        break
    except Exception:
        pass
    # 2. Supplement with Google News RSS for ra.co/news
    gn = fetch_gnews_fallback("site:ra.co/news OR site:ra.co/features", "Resident Advisor", "Electronic Music & DJ", limit=4)
    items.extend(gn)
    return items[:5], "L2_HTML_PLUS_L3_GNEWS", url


def fetch_music_sos():
    items = []
    for section in ("news", "techniques"):
        url = f"https://www.soundonsound.com/{section}"
        try:
            r = polite_get(url, referer="https://www.soundonsound.com/")
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "lxml")
                for h in soup.select("h2 a, h3 a"):
                    href = h.get("href", "")
                    txt = clean_text(h.get_text(" ", strip=True), 160)
                    if href.startswith(f"/{section}/") and not href.endswith(".jpg") and len(txt) > 10:
                        items.append({
                            "source": "Sound On Sound",
                            "category": f"SOS {section.capitalize()}",
                            "title": txt,
                            "url": "https://www.soundonsound.com" + href,
                            "published": datetime.now(CST).strftime("%Y-%m-%d"),
                            "summary": f"Sound On Sound {section}: {txt}",
                        })
                        if len([x for x in items if x["category"].endswith(section.capitalize())]) >= 3:
                            break
        except Exception:
            pass
    if items:
        return items[:5], "L2_CURL_CFFI_HTML", "https://www.soundonsound.com/news"
    return fetch_gnews_fallback("site:soundonsound.com", "Sound On Sound", "Pro Audio", 4), "L3_GNEWS_RSS_FALLBACK", "gnews"


def run_music_bot():
    tasks = [
        ("Attack Magazine", lambda: fetch_rss_with_fallback("Attack Magazine", "Production Technique", ["https://www.attackmagazine.com/feed/"], "site:attackmagazine.com", 5)),
        ("Resident Advisor", fetch_music_ra),
        ("CDM", lambda: fetch_rss_with_fallback("CDM", "Synths & Creative Tech", ["https://cdm.link/feed/"], "site:cdm.link", 5)),
        ("DJ TechTools", lambda: fetch_rss_with_fallback("DJ TechTools", "DJ Gear & Skills", ["https://djtechtools.com/feed/"], "site:djtechtools.com", 5)),
        ("Bedroom Producers Blog", lambda: fetch_rss_with_fallback("Bedroom Producers Blog", "Free VST & Samples", ["https://bedroomproducersblog.com/feed/"], "site:bedroomproducersblog.com", 6)),
        ("Sound On Sound", fetch_music_sos),
        ("MaxforLive.com", lambda: fetch_rss_with_fallback("MaxforLive.com", "Ableton M4L Devices", [], "site:maxforlive.com/library/device", 5)),
        ("KVR Audio", lambda: fetch_rss_with_fallback("KVR Audio", "VST Plugins", ["https://www.kvraudio.com/rss/kvr_news.rss"], "site:kvraudio.com/news", 5)),
        ("Synthtopia", lambda: fetch_rss_with_fallback("Synthtopia", "Hardware & Synths", ["https://www.synthtopia.com/feed/"], "site:synthtopia.com", 5)),
        ("MusicRadar", lambda: fetch_rss_with_fallback("MusicRadar", "Music Tech", ["https://www.musicradar.com/feeds/all"], "site:musicradar.com", 4)),
    ]
    return execute_bot_tasks("music", tasks, post=enrich_generic)


# ==============================================================================
# 5. COMMERCE & ARBITRAGE BOT SOURCES
# ==============================================================================
def _extract_reddit_entries(entries, sub: str, category: str, limit: int, filter_sub: bool = False):
    items = []
    sub_lower = sub.lower()
    for e in entries:
        link = getattr(e, "link", "")
        if filter_sub and f"/r/{sub_lower}/" not in link.lower():
            continue
        title = clean_text(getattr(e, "title", ""), 160)
        tl = title.lower()
        if any(skip in tl for skip in ["weekly", "discussion hub", "how was your gig", "show us what you", "megathread"]):
            continue
        raw_desc = getattr(e, "summary", "") or (e.content[0].get("value", "") if hasattr(e, "content") and e.content else "")
        summary = clean_text(raw_desc, 500)
        summary = re.sub(r"submitted by\s+/u/\S+\s*\[link\]\s*\[comments\]", "", summary, flags=re.I).strip()
        items.append({
            "source": f"r/{sub}",
            "category": category,
            "title": title,
            "url": link,
            "published": format_date(e),
            "summary": summary if summary else f"Active thread in r/{sub}",
        })
        if len(items) >= limit:
            break
    return items


def fetch_reddit_sub(sub: str, category: str, limit: int = 4, enrich_scores: bool = False):
    """Reddit via public Arctic Shift archive API (reddit.com blocks cloud servers)."""
    after = (datetime.now(timezone.utc) - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%S")
    endpoint = f"https://arctic-shift.photon-reddit.com/api/posts/search?subreddit={sub}&after={after}&limit=100&sort=desc"
    if sub == "popular":
        return [], "SKIPPED_NO_POPULAR_IN_ARCTIC", endpoint
    skip = ["weekly", "discussion hub", "how was your gig", "show us what you", "megathread", "daily thread", "what are you working on"]
    for _ in range(3):
        try:
            r = polite_get(endpoint, referer="https://arctic-shift.photon-reddit.com/", extra_headers={"Accept": "application/json"}, timeout=40)
            if r.status_code == 429:
                time.sleep(min(int(float(r.headers.get("x-ratelimit-reset", 20))) + 1, 60))
                continue
            if r.status_code != 200:
                time.sleep(2)
                continue
            data = r.json()
            posts = data.get("data") or [] if isinstance(data, dict) else data
            items = []
            for p in posts:
                if p.get("stickied") or p.get("pinned") or p.get("removed_by_category") or p.get("over_18"):
                    continue
                title = clean_text(p.get("title") or "", 160)
                if not title or any(k in title.lower() for k in skip):
                    continue
                created = p.get("created_utc") or 0
                items.append({
                    "source": f"r/{sub}",
                    "category": category,
                    "title": title,
                    "url": "https://www.reddit.com" + (p.get("permalink") or f"/r/{sub}/comments/{p.get('id')}/"),
                    "published": datetime.fromtimestamp(created, CST).strftime("%Y-%m-%d %H:%M CST") if created else "",
                    "summary": clean_text(f"score={p.get('score') or 0} comments={p.get('num_comments') or 0} | " + (p.get("selftext") or ""), 600),
                    "_rank": ((p.get("score") or 0), (p.get("num_comments") or 0)),
                })
            items.sort(key=lambda x: x["_rank"], reverse=True)
            for it in items:
                it.pop("_rank", None)
            return items[:limit], "L2_ARCTIC_SHIFT", endpoint
        except Exception:
            time.sleep(2)
    return [], "FAILED", endpoint


def fetch_reddit_embed_score(permalink: str):
    embed_url = permalink.replace("https://www.reddit.com", "https://www.redditmedia.com") + "?ref_source=embed&ref=share&embed=true"
    try:
        r = creq.get(embed_url, impersonate="chrome", timeout=8)
        if r.status_code == 200:
            up_m = re.search(r"([0-9][0-9,\.kK]*)\s*upvotes?</span", r.text, re.I)
            cm_m = re.search(r"([0-9][0-9,\.kK]*)\s*comments?</span", r.text, re.I)
            return (up_m.group(1) if up_m else "0"), (cm_m.group(1) if cm_m else "0")
    except Exception:
        pass
    return "0", "0"


def fetch_commerce_hn():
    url = "https://hn.algolia.com/api/v1/search_by_date?query=Show+HN+OR+SaaS+OR+SEO&tags=story&hitsPerPage=6"
    r = polite_get(url, referer="https://news.ycombinator.com/")
    data = r.json()
    items = []
    for hit in data.get("hits", [])[:5]:
        oid = hit.get("objectID")
        link = hit.get("url") or f"https://news.ycombinator.com/item?id={oid}"
        items.append({
            "source": "Hacker News (Show HN)",
            "category": "Indie Monetization",
            "title": clean_text(hit.get("title", ""), 160),
            "url": link,
            "published": str(hit.get("created_at", ""))[:19],
            "summary": f"Show HN / Monetization: {hit.get('points', 0)} pts, {hit.get('num_comments', 0)} comments",
        })
    return items, "L1_OFFICIAL_API", url


def run_commerce_bot():
    tasks = [
        ("BlackHatWorld", lambda: fetch_rss_with_fallback("BlackHatWorld", "BlackHat SEO & IM", ["https://www.blackhatworld.com/forums/-/index.rss"], "site:blackhatworld.com/seo", 5)),
        ("BuilderSociety", lambda: fetch_rss_with_fallback("BuilderSociety", "Traffic Leaking & Growth", ["https://www.buildersociety.com/forums/-/index.rss"], "site:buildersociety.com", 5)),
        ("Search Engine Roundtable", lambda: fetch_rss_with_fallback("Search Engine Roundtable", "SEO & Google Updates", [], "site:seroundtable.com", 5)),
        ("Detailed & Ahrefs", lambda: fetch_rss_with_fallback("Detailed & Ahrefs", "SEO Case Studies", ["https://ahrefs.com/blog/feed/", "https://detailed.com/feed/"], "site:ahrefs.com/blog", 5)),
        ("affLIFT", lambda: fetch_rss_with_fallback("affLIFT", "Affiliate Arbitrage", ["https://afflift.com/forums/-/index.rss"], "site:afflift.com/f/", 4)),
        ("AffiliateFix", lambda: fetch_rss_with_fallback("AffiliateFix", "CPA & Media Buying", ["https://www.affiliatefix.com/forums/-/index.rss"], "site:affiliatefix.com/threads", 4)),
        ("Doctor of Credit", lambda: fetch_rss_with_fallback("Doctor of Credit", "Bank & Card Bonuses", ["https://www.doctorofcredit.com/feed/"], "site:doctorofcredit.com", 5)),
        ("Slickdeals", lambda: fetch_rss_with_fallback("Slickdeals", "Deal & GC Arbitrage", ["https://slickdeals.net/newsearch.php?mode=frontpage&searcharea=deals&searchin=first&rss=1"], "site:slickdeals.net", 5)),
        ("r/blackhat", lambda: fetch_reddit_sub("blackhat", "BlackHat Reddit", 3)),
        ("r/bigseo", lambda: fetch_reddit_sub("bigseo", "SEO Reddit", 3)),
        ("r/SEO", lambda: fetch_reddit_sub("SEO", "SEO Reddit", 3)),
        ("r/affiliatemarketing", lambda: fetch_reddit_sub("affiliatemarketing", "Affiliate Reddit", 3)),
        ("r/churning", lambda: fetch_reddit_sub("churning", "Churning Reddit", 3)),
        ("r/beermoney", lambda: fetch_reddit_sub("beermoney", "BeerMoney Reddit", 3)),
        ("r/Flipping", lambda: fetch_reddit_sub("Flipping", "Reselling Arbitrage", 3)),
        ("Hacker News Show HN", fetch_commerce_hn),
        ("Search Engine Land", lambda: fetch_rss_with_fallback("Search Engine Land", "SEO & Google Updates", ["https://searchengineland.com/feed"], "site:searchengineland.com", 4)),
        ("Empire Flippers", lambda: fetch_rss_with_fallback("Empire Flippers", "Digital Asset M&A", ["https://empireflippers.com/feed/"], "site:empireflippers.com", 4)),
        ("Flippa Blog", lambda: fetch_rss_with_fallback("Flippa Blog", "Digital Asset M&A", ["https://flippa.com/blog/feed/"], "site:flippa.com/blog", 4)),
    ]
    return execute_bot_tasks("commerce", tasks, post=enrich_generic)


# ==============================================================================
# 6. REDDIT BOT SOURCES (21 Subreddits + r/popular)
# ==============================================================================
def run_reddit_bot():
    subs = [
        ("Ableton", "Music & DJ"),
        ("edmproduction", "Music & DJ"),
        ("synthesizers", "Music & DJ"),
        ("WeAreTheMusicMakers", "Music & DJ"),
        ("Beatmatch", "Music & DJ"),
        ("DJs", "Music & DJ"),
        ("ClaudeAI", "AI & Vibe Coding"),
        ("SideProject", "AI & Vibe Coding"),
        ("ChatGPTCoding", "AI & Vibe Coding"),
        ("LocalLLaMA", "AI & Vibe Coding"),
        ("Singularity", "AI & Vibe Coding"),
        ("aivideo", "AI Audio & Video"),
        ("SunoAI", "AI Audio & Video"),
        ("SaaS", "SaaS & Indie Monetization"),
        ("indiehackers", "SaaS & Indie Monetization"),
        ("microsaas", "SaaS & Indie Monetization"),
        ("juststart", "SaaS & Indie Monetization"),
        ("EntrepreneurRideAlong", "SaaS & Indie Monetization"),
        ("blackhat", "SEO & Growth"),
        ("bigseo", "SEO & Growth"),
        ("SEO", "SEO & Growth"),
        ("affiliatemarketing", "SEO & Growth"),
        ("digital_marketing", "SEO & Growth"),
        ("churning", "Arbitrage & Hustle"),
        ("beermoney", "Arbitrage & Hustle"),
        ("SideHustle", "Arbitrage & Hustle"),
        ("Entrepreneur", "Arbitrage & Hustle"),
        ("popular", "Reddit Frontpage"),
    ]
    tasks = []
    for sub, cat in subs:
        lim = 15 if sub == "popular" else 4
        tasks.append((f"r/{sub}", lambda s=sub, c=cat, l=lim: fetch_reddit_sub(s, c, limit=l, enrich_scores=True)))
    return execute_bot_tasks("reddit", tasks, max_workers=2, post=enrich_generic)




# ==============================================================================
# 7. EVENTS & CULTURAL LIFE BOT (演出与艺文 — GZ prioritized, SZ, SH Clubs)
# ==============================================================================
def _load_venues_blacklist():
    db_path = os.path.join(STATE_DIR, "venues_db.json")
    if os.path.exists(db_path):
        try:
            with open(db_path, "r", encoding="utf-8") as f:
                bl = json.load(f).get("blacklist", {})
                words = (
                    bl.get("closed_venues", [])
                    + bl.get("excluded_tacky_or_commercial_venues", [])
                    + bl.get("excluded_event_keywords", [])
                )
                return [w.lower() for w in words]
        except Exception:
            pass
    return ["云上咖啡", "音乐唐人馆", "胡桃里", "贰麻酒馆", "海伦司", "少儿", "亲子", "相亲"]


def _is_blacklisted_event(text: str, bl_words: list) -> bool:
    tl = text.lower()
    return any(w in tl for w in bl_words)


def fetch_localhub_city(city_slug: str, city_cn: str, limit: int = 25):
    """LocalHub 按场地列出近期演出：每条取 演出名 · 艺人 · 日期 · 场地。"""
    url = f"https://localhub.to/{city_slug}?lang=zh"
    r = polite_get(url, referer="https://localhub.to/")
    soup = BeautifulSoup(r.text, "lxml")
    bl = _load_venues_blacklist()
    items, seen = [], set()
    for blk in soup.select("div.venue-block"):
        shows = blk.select_one("div.venue-shows")
        venue = ""
        for node in blk.find_all(string=True):
            if shows and node.find_parent("div", class_="venue-shows"):
                continue
            venue = clean_text(str(node), 60)
            if venue:
                break
        for show in blk.select("div.venue-show-item"):
            a = show.select_one("a.venue-show-title-link") or show.find("a", href=True)
            if not a:
                continue
            title = clean_text(a.get_text(" ", strip=True), 140)
            full = clean_text(show.get_text(" ", strip=True), 260)
            m = re.search(r"(\d{1,2}/\d{1,2})\s*(周[一二三四五六日])?", full)
            date = m.group(0) if m else ""
            artists = clean_text(full.replace(title, "", 1).replace(date, "").strip(" •,|"), 120)
            if not title or title in seen or _is_blacklisted_event(title + " " + venue, bl):
                continue
            seen.add(title)
            href = a.get("href", "")
            items.append({
                "source": f"LocalHub ({city_cn})",
                "category": f"{city_cn} · 现场与地下俱乐部",
                "title": title,
                "url": href if href.startswith("http") else f"https://localhub.to{href}",
                "published": datetime.now(CST).strftime("%Y-%m-%d"),
                "summary": f"日期：{date or '未注明'} · 场地：{venue or '未注明'}" + (f" · 艺人：{artists}" if artists else ""),
            })
            if len(items) >= limit:
                return items, "L2_CURL_CFFI_HTML", url
    return items, "L2_CURL_CFFI_HTML", url


def fetch_showstart_city(city_code: str, city_cn: str):
    url = f"https://www.showstart.com/event/list?cityCode={city_code}"
    r = polite_get(url, referer="https://www.showstart.com/")
    soup = BeautifulSoup(r.text, "lxml")
    bl = _load_venues_blacklist()
    items = []
    for a in soup.select("a[href*='/event/']"):
        txt = clean_text(a.get_text(" ", strip=True), 150)
        if len(txt) < 6 or _is_blacklisted_event(txt, bl):
            continue
        mc = re.search(r"\[([^\]]{2,6})\]", txt)
        if mc and mc.group(1) != city_cn:
            continue  # 别的城市的活动
        md = re.search(r"(\d{4})/(\d{2})/(\d{2})", txt)
        if md and md.group(0) < datetime.now(CST).strftime("%Y/%m/%d"):
            continue  # 已经过去的活动
        href = a.get("href", "")
        full_url = href if href.startswith("http") else f"https://www.showstart.com{href}"
        items.append({
            "source": f"秀动 Showstart ({city_cn})",
            "category": f"{city_cn} · 独立Livehouse",
            "title": txt,
            "url": full_url,
            "published": datetime.now(CST).strftime("%Y-%m-%d"),
            "summary": f"秀动网{city_cn}站近期独立音乐现场排期（已过滤黑名单场馆）",
        })
        if len(items) >= 6:
            break
    if not items:
        return fetch_rss_with_fallback(f"秀动 ({city_cn})", f"{city_cn} · 独立Livehouse", [], f"site:showstart.com {city_cn} Livehouse 2026", 4)
    return items, "L2_CURL_CFFI_HTML", url


def fetch_douban_location(city_slug: str, city_cn: str):
    url = f"https://www.douban.com/location/{city_slug}/events/week-all"
    r = polite_get(url, referer="https://www.douban.com/")
    soup = BeautifulSoup(r.text, "lxml")
    bl = _load_venues_blacklist()
    items = []
    for li in soup.select("li.list-entry, div.info")[:12]:
        a = li.select_one("div.title a, h3 a, a")
        if not a:
            continue
        title = clean_text(a.get_text(" ", strip=True), 140)
        if not title or _is_blacklisted_event(title, bl):
            continue
        href = a.get("href", url)
        meta = clean_text(li.get_text(" ", strip=True), 160)
        if _is_blacklisted_event(meta, bl):
            continue
        items.append({
            "source": f"豆瓣同城 ({city_cn})",
            "category": f"{city_cn} · 展览/沙龙/放映/演出",
            "title": title,
            "url": href,
            "published": datetime.now(CST).strftime("%Y-%m-%d"),
            "summary": meta,
        })
        if len(items) >= 6:
            break
    return items, "L2_CURL_CFFI_HTML", url


def fetch_b10_live():
    url = "https://b10live.cn/events"
    r = polite_get(url, referer="https://b10live.cn/")
    soup = BeautifulSoup(r.text, "lxml")
    items = []
    for article in soup.select("article, .eventlist-event, .summary-item")[:6]:
        a = article.select_one("a[href]")
        txt = clean_text(article.get_text(" ", strip=True), 160)
        if not txt or len(txt) < 8:
            continue
        href = a.get("href", "") if a else ""
        full_url = href if href.startswith("http") else f"https://b10live.cn{href}"
        items.append({
            "source": "深圳 B10 现场 (b10live.cn)",
            "category": "深圳 · 独立Livehouse与爵士节",
            "title": txt[:120],
            "url": full_url,
            "published": datetime.now(CST).strftime("%Y-%m-%d"),
            "summary": txt,
        })
    return items, "L2_CURL_CFFI_HTML", url


def run_events_bot():
    tasks = [
        ("LocalHub 广州", lambda: fetch_localhub_city("guangzhou", "广州")),
        ("秀动 Showstart 广州", lambda: fetch_showstart_city("20", "广州")),
        ("豆瓣同城 广州", lambda: fetch_douban_location("guangzhou", "广州")),
        ("LocalHub 深圳", lambda: fetch_localhub_city("shenzhen", "深圳")),
        ("B10 现场 (深圳)", fetch_b10_live),
        ("秀动 Showstart 深圳", lambda: fetch_showstart_city("28", "深圳")),
        ("豆瓣同城 深圳", lambda: fetch_douban_location("shenzhen", "深圳")),
        ("LocalHub 上海 (地下电子Club)", lambda: fetch_localhub_city("shanghai", "上海")),
        ("广深美术馆与独立书店动态", lambda: fetch_rss_with_fallback("广深艺文检索", "广州/深圳 · 书店与美术馆", [], "广州 OR 深圳 时代美术馆 OR 博尔赫斯书店 OR 广东美术馆 OR 海上世界文化艺术中心 展览 2026", 5)),
    ]
    payload = execute_bot_tasks("events", tasks)
    # Persist seen_events.json for deduplication tracking
    seen_path = os.path.join(STATE_DIR, "seen_events.json")
    seen = {}
    if os.path.exists(seen_path):
        try:
            with open(seen_path, "r", encoding="utf-8") as f:
                seen = json.load(f)
        except Exception:
            seen = {}
    for it in payload.get("items", []):
        seen[it["title"][:80]] = {"url": it["url"], "first_seen": payload["fetched_at"]}
    with open(seen_path, "w", encoding="utf-8") as f:
        json.dump(seen, f, indent=2, ensure_ascii=False)
    return payload


# ==============================================================================
# 8. MUSIC RELEASES BOT (音乐发行 — RYM / Metacritic / AOTY / Beatport / Vinyl / Awards)
# ==============================================================================
def fetch_beatport_underground():
    genres = [
        ("raw-deep-hypnotic-techno", "92", "Raw/Deep/Hypnotic Techno"),
        ("leftfield-house-and-techno", "85", "Leftfield House & Techno"),
        ("electronica", "3", "Electronica / IDM"),
        ("uk-garage-bassline", "86", "UK Garage / Bassline"),
        ("indie-dance", "37", "Indie Dance"),
    ]
    items = []
    for slug, gid, gname in genres:
        url = f"https://www.beatport.com/genre/{slug}/{gid}/top-100"
        try:
            r = polite_get(url, referer="https://www.beatport.com/")
            if r.status_code != 200:
                continue
            m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', r.text, re.S)
            if not m:
                continue
            data = json.loads(m.group(1))
            queries = data.get("props", {}).get("pageProps", {}).get("dehydratedState", {}).get("queries", [])
            for q in queries:
                res = q.get("state", {}).get("data", {}).get("results", [])
                if isinstance(res, list) and res and "bpm" in res[0]:
                    for rank, tr in enumerate(res[:3], 1):
                        t_name = tr.get("name", "")
                        mix = tr.get("mix_name", "Original Mix")
                        artists = ", ".join(a.get("name", "") for a in tr.get("artists", []))
                        bpm = tr.get("bpm", "")
                        key_obj = tr.get("key") or {}
                        key_name = key_obj.get("name", "") if isinstance(key_obj, dict) else ""
                        label = (tr.get("release") or {}).get("label", {}).get("name", "")
                        pub_date = tr.get("publish_date", "")
                        t_slug = tr.get("slug", "track")
                        t_id = tr.get("id", "")
                        t_url = f"https://www.beatport.com/track/{t_slug}/{t_id}" if t_id else url
                        items.append({
                            "source": f"Beatport ({gname})",
                            "category": "必听新单曲与DJ舞池热曲",
                            "title": f"[{gname} #{rank}] {artists} — {t_name} ({mix})",
                            "url": t_url,
                            "published": str(pub_date)[:10],
                            "summary": f"BPM: {bpm} | Key: {key_name} | 厂牌: {label} | Beatport {gname} 周销量榜 #{rank}",
                        })
                    break
        except Exception:
            pass
    return items, "L1_EMBEDDED_JSON", "https://www.beatport.com/"


def fetch_hardwax_vinyl():
    url = "https://hardwax.com/this-week/"
    r = polite_get(url, referer="https://hardwax.com/")
    soup = BeautifulSoup(r.text, "lxml")
    items = []
    for blk in soup.select("div.block, article, .record")[:8]:
        txt = clean_text(blk.get_text(" ", strip=True), 160)
        if len(txt) < 12:
            continue
        a = blk.select_one("a[href]")
        href = a.get("href", "") if a else ""
        full_url = href if href.startswith("http") else f"https://hardwax.com{href}"
        items.append({
            "source": "Hard Wax (柏林黑胶唱片行)",
            "category": "电子与俱乐部 (Electronic/IDM/Techno)",
            "title": txt[:110],
            "url": full_url,
            "published": datetime.now(CST).strftime("%Y-%m-%d"),
            "summary": f"Hard Wax 本周新上架黑胶推荐：{txt}",
        })
    return items, "L2_CURL_CFFI_HTML", url


def fetch_douban_music_chart():
    url = "https://music.douban.com/chart"
    r = polite_get(url, referer="https://music.douban.com/")
    soup = BeautifulSoup(r.text, "lxml")
    items = []
    for li in soup.select("ul.col5 li, .intro")[:8]:
        a = li.select_one("a[href*='subject']") or li.select_one("a[href]")
        txt = clean_text(li.get_text(" ", strip=True), 140)
        if not txt or len(txt) < 6:
            continue
        href = a.get("href", url) if a else url
        items.append({
            "source": "豆瓣音乐 (本周口碑榜)",
            "category": "华语与亚洲高分",
            "title": txt[:110],
            "url": href,
            "published": datetime.now(CST).strftime("%Y-%m-%d"),
            "summary": f"豆瓣音乐本周流行与口碑新碟榜：{txt}",
        })
    return items, "L2_CURL_CFFI_HTML", url


def run_releases_bot():
    tasks = [
        ("Beatport 地下电子分榜 (含BPM与调性)", fetch_beatport_underground),
        ("Pitchfork 新专评测 & Best New Music", lambda: fetch_rss_with_fallback("Pitchfork Reviews", "高分新专辑与EP", ["https://pitchfork.com/feed/feed-album-reviews/rss"], "site:pitchfork.com/reviews/albums 2026", 8)),
        ("Stereogum 新歌与重磅推荐", lambda: fetch_rss_with_fallback("Stereogum", "独立与摇滚 (Indie/Post-Punk/Shoegaze)", ["https://www.stereogum.com/feed/"], "site:stereogum.com Heavy Rotation 2026", 6)),
        ("Bandcamp Daily (Album of the Day & 地下电子)", lambda: fetch_rss_with_fallback("Bandcamp Daily", "电子与独立唱片行推荐", ["https://daily.bandcamp.com/feed"], "site:daily.bandcamp.com Album of the Day 2026", 6)),
        ("Hard Wax 柏林黑胶周榜", fetch_hardwax_vinyl),
        ("Billboard (200 / Hot 100 / Electronic / Vinyl)", lambda: fetch_rss_with_fallback("Billboard Charts", "大众破圈与Billboard", ["https://www.billboard.com/feed/"], "site:billboard.com/charts Billboard 200 OR Dance Electronic 2026", 6)),
        ("豆瓣音乐本周口碑榜", fetch_douban_music_chart),
        ("r/indieheads [FRESH ALBUM]", lambda: fetch_reddit_sub("indieheads", "Reddit 独立新专热议", 5)),
        ("r/electronicmusic [FRESH]", lambda: fetch_reddit_sub("electronicmusic", "Reddit 电子新碟热议", 5)),
        ("r/hiphopheads [FRESH]", lambda: fetch_reddit_sub("hiphopheads", "Reddit 嘻哈/R&B新专", 4)),
    ]
    payload = execute_bot_tasks("releases", tasks, post=enrich_generic)
    seen_path = os.path.join(STATE_DIR, "seen_releases.json")
    seen = {}
    if os.path.exists(seen_path):
        try:
            with open(seen_path, "r", encoding="utf-8") as f:
                seen = json.load(f)
        except Exception:
            seen = {}
    for it in payload.get("items", []):
        seen[it["title"][:80]] = {"url": it["url"], "first_seen": payload["fetched_at"]}
    with open(seen_path, "w", encoding="utf-8") as f:
        json.dump(seen, f, indent=2, ensure_ascii=False)
    return payload

# ==============================================================================
# 9. CHIANG MAI EVENTS BOT (清迈活动 — 未来 7 天；电子乐 / 艺术 / 电影 / 文学 / 社交)
# ==============================================================================
ICT = timezone(timedelta(hours=7))  # 清迈时间
CM_DAYS_AHEAD = 7
STEVE_DOC_ID = "15MtcTuLt7biryCT5kFQ0bg4wseXBZ8CIPplIXza8JuA"  # tiny.cc/stevesemail 指向的 Google 文档
CM_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], 1)}
CM_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
CM_WEEKDAYS_CN = ["每周一", "每周二", "每周三", "每周四", "每周五", "每周六", "每周日"]
# 商业推广（酒店自助餐、水疗、健身课等）直接去掉，省 AI 费用
CM_EXCLUDE = ["buffet", "set dinner", "dinner cruise", "brunch", "spa ", "massage", "golf", "crossfit",
              "padel", "bridge club", "rugby", "football", "aquatic", "mobility", "hiit", "bingo",
              "christ-centered", "40+", "women’s lunch", "promotion"]
# 每周固定活动太多，只留和你兴趣有关的
CM_WEEKLY_KEEP = ["dj", "techno", "house", "rave", "jam", "open mic", "live music", "music night", "jazz",
                  "blues", "vinyl", "record", "film", "cinema", "movie", "screening", "book", "writ", "poetry",
                  "art", "sketch", "gallery", "comedy", "improv", "language exchange", "exchange", "quiz",
                  "trivia", "meetup", "social", "hostel", "nomad", "game night", "board game", "salsa", "bachata"]


def _cm_date(month_name: str, day: str, today):
    m = CM_MONTHS.get(month_name.lower())
    if not m:
        return None
    year = today.year
    if m < today.month - 6:   # 11 月看到 1 月 → 明年
        year += 1
    elif m > today.month + 6:
        year -= 1
    try:
        return datetime(year, m, int(day)).date()
    except ValueError:
        return None


_CM_DATE_RE = re.compile(
    r"^(?P<until>now\s+until\s+)?(?P<m1>[A-Za-z]+)\s+(?P<d1>\d{1,2})(?:st|nd|rd|th)?"
    r"(?:\s*(?:–|—|-|&|and|to|until)\s*(?:(?P<m2>[A-Za-z]+)\s+)?(?P<d2>\d{1,2}))?",
    re.I,
)


def _cm_first_url(text: str, fallback: str) -> str:
    m = re.search(r"https?://[^\s)\]]+", text)
    return m.group(0).rstrip(".,;") if m else fallback


def fetch_cm_steve_list():
    """Steve 的清迈活动清单（本地老外每周更新的 Google 文档，400+ 活动）。"""
    url = f"https://docs.google.com/document/d/{STEVE_DOC_ID}/export?format=txt"
    page = f"https://docs.google.com/document/d/{STEVE_DOC_ID}/mobilebasic"
    r = polite_get(url, referer="https://docs.google.com/")
    if r.status_code != 200:
        return [], "FAILED", url
    today = datetime.now(ICT).date()
    end = today + timedelta(days=CM_DAYS_AHEAD - 1)
    items, mode, weekday = [], "dated", None
    n_dated = n_weekly = 0
    for raw in r.text.splitlines():
        line = re.sub(r"\s+", " ", raw.replace("﻿", "")).strip()
        if not line:
            continue
        low = line.lower()
        if "weekly events that seldom change" in low:
            mode = "weekly"
            continue
        if mode == "weekly" and low.startswith("notices"):
            break
        if any(w in low for w in CM_EXCLUDE):
            continue
        if mode == "weekly":
            if line in CM_WEEKDAYS:
                weekday = CM_WEEKDAYS.index(line)
                continue
            if weekday is None or not any(k in low for k in CM_WEEKLY_KEEP):
                continue
            label = f"{CM_WEEKDAYS_CN[weekday]}（固定活动）"
        else:
            m = _CM_DATE_RE.match(line)
            if not m:
                continue
            d1 = _cm_date(m.group("m1"), m.group("d1"), today)
            if not d1:
                continue
            if m.group("d2"):
                d2 = _cm_date(m.group("m2") or m.group("m1"), m.group("d2"), today) or d1
            else:
                d2 = d1
            if m.group("until"):
                d1, d2 = today, d1
            if d2 < today or d1 > end:
                continue
            label = f"{max(d1, today):%m-%d}" + (f" ~ {d2:%m-%d}" if d2 != d1 else "")
        if mode == "weekly" and n_weekly >= 50:
            continue
        if mode == "dated" and n_dated >= 100:
            continue
        n_weekly += mode == "weekly"
        n_dated += mode == "dated"
        items.append({
            "source": "Steve's Chiang Mai List",
            "category": "清迈每周固定活动" if mode == "weekly" else "清迈未来7天活动",
            "title": clean_text(line, 70),
            "url": _cm_first_url(line, page),
            "published": label,
            "summary": clean_text(line, 200 if mode == "weekly" else 300),
        })
    return items, "L2_GDOC_TXT", url


def fetch_cm_timeout():
    """Time Out 清迈：本月和上月的活动 / 艺术 / 新闻文章，只取最近 3 周发的。"""
    now = datetime.now(ICT)
    prev = (now.replace(day=1) - timedelta(days=1))
    links = []
    for y, m in ((now.year, now.month), (prev.year, prev.month)):
        sm = f"https://www.timeout.com/chiang-mai/sitemaps/{y}/{m:02d}"
        try:
            r = polite_get(sm, referer="https://www.timeout.com/chiang-mai")
            if r.status_code != 200:
                continue
            for href in re.findall(r'href="(https://www\.timeout\.com/chiang-mai/(?:things-to-do|art|news|music|film|nightlife|theatre)/[^"#?]+)"', r.text):
                if href.endswith("/weekly-event") or href in links:
                    continue
                links.append(href)
        except Exception:
            pass
    items = []
    cutoff = now - timedelta(days=21)
    for href in links[:30]:
        if len(items) >= 15:
            break
        try:
            r = polite_get(href, referer="https://www.timeout.com/chiang-mai")
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "lxml")
            t = soup.find("time", attrs={"datetime": True})
            pub = t["datetime"][:10] if t else ""
            if t:
                try:
                    if datetime.fromisoformat(t["datetime"]) < cutoff:
                        continue
                except ValueError:
                    pass
            og = soup.find("meta", property="og:title")
            title = (og.get("content", "") if og else "").split(" | ")[0]
            text = soup.get_text("\n", strip=True)
            body = ""
            if "Time Out says" in text:
                body = text.split("Time Out says", 1)[1].split("\nDetails", 1)[0].split("\nAdvertising", 1)[0]
            body = re.sub(r"\s+", " ", body).strip()
            if not body:
                desc = soup.find("meta", attrs={"name": "description"})
                body = desc.get("content", "") if desc else ""
            if len(body) > 520:  # 保留开头介绍 + 结尾那行（日期、票价、地点）
                body = body[:300] + " … " + body[-200:]
            if not title:
                continue
            items.append({
                "source": "Time Out Chiang Mai",
                "category": "清迈活动文章",
                "title": clean_text(title, 160),
                "url": href,
                "published": pub,
                "summary": body or title,
            })
        except Exception:
            pass
    return items, "L2_CURL_CFFI_HTML", "https://www.timeout.com/chiang-mai/sitemaps"


def fetch_cm_gnews():
    items = []
    for q, cat in [
        ('"Chiang Mai" (techno OR DJ OR rave OR club OR "live music" OR jazz) when:14d', "新闻搜索 · 音乐与夜生活"),
        ('"Chiang Mai" (exhibition OR gallery OR film OR cinema OR "book fair" OR festival) when:14d', "新闻搜索 · 艺术与节庆"),
    ]:
        try:
            items.extend(fetch_gnews_fallback(q, "Google News 搜索", cat, limit=6))
        except Exception:
            pass
    return items, "L3_GNEWS_SEARCH", "gnews:Chiang Mai"


def run_chiangmai_bot():
    tasks = [
        ("Steve's Chiang Mai List", fetch_cm_steve_list),
        ("Time Out Chiang Mai", fetch_cm_timeout),
        ("Google News 搜索", fetch_cm_gnews),
    ]
    return execute_bot_tasks("chiangmai", tasks)


# ==============================================================================
# EXECUTOR & OUTPUT GENERATOR
# ==============================================================================
def execute_bot_tasks(bot_name: str, tasks: list, max_workers: int = 3, post=None):
    t0 = time.time()
    all_items = []
    source_stats = []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_map = {pool.submit(fn): name for name, fn in tasks}
        for fut in as_completed(future_map):
            src_name = future_map[fut]
            try:
                items, layer, endpoint = fut.result()
                for it in items:
                    it.setdefault("task", src_name)
                source_stats.append({
                    "source": src_name,
                    "status": "ok" if items else "empty",
                    "layer": layer,
                    "count": len(items),
                    "endpoint": endpoint,
                })
                all_items.extend(items)
            except Exception as e:
                source_stats.append({
                    "source": src_name,
                    "status": f"error: {e}",
                    "layer": "ERROR",
                    "count": 0,
                    "endpoint": "",
                })

    # Preserve task order
    order_map = {name: i for i, (name, _) in enumerate(tasks)}
    source_stats.sort(key=lambda x: order_map.get(x["source"], 999))
    all_items.sort(key=lambda it: order_map.get(it.get("task"), 999))  # 稳定顺序（sort 不打乱同一来源内部顺序）

    # Merge built-in tool supplements (x / WebSearch) if present
    supp_file = os.path.join( os.path.dirname(__file__), "builtin_supplements.json")
    if os.path.exists(supp_file):
        try:
            with open(supp_file, "r", encoding="utf-8") as sf:
                supps = json.load(sf).get(bot_name, [])
            if supps:
                from collections import Counter
                grouped = Counter()
                ep_map = {}
                for item in supps:
                    all_items.append(item)
                    key = (item["source"], item.get("layer", "L4_BUILTIN_SUPPLEMENT"))
                    grouped[key] += 1
                    ep_map[key] = item["url"]
                for (src_n, lyr), cnt in grouped.items():
                    source_stats.append({
                        "source": src_n,
                        "status": "ok",
                        "layer": lyr,
                        "count": cnt,
                        "endpoint": ep_map[(src_n, lyr)],
                    })
        except Exception as e:
            print(f"[WARN] Failed loading builtin_supplements.json: {e}")

    if post:
        try:
            post(all_items)
        except Exception as e:
            print(f"[WARN] {bot_name} 加料步骤出错（不影响报告）: {e}")
    for idx, it in enumerate(all_items, 1):
        it["id"] = idx
        if is_paywalled(it.get("url", "")):
            it["paywall"] = True

    elapsed = round(time.time() - t0, 2)
    payload = {
        "bot": bot_name,
        "fetched_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S CST"),
        "elapsed_seconds": elapsed,
        "sources_total": len(source_stats),
        "sources_succeeded": sum(1 for s in source_stats if s["count"] > 0),
        "total_items": len(all_items),
        "source_stats": source_stats,
        "items": all_items,
    }

    json_path = os.path.join(OUTPUT_DIR, f"{bot_name}_latest.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    md_lines = [
        f"# {bot_name.upper()} Daily Pre-Fetch Brief ({payload['fetched_at']})",
        f"- Sources succeeded: {payload['sources_succeeded']}/{payload['sources_total']} | Total items: {payload['total_items']} | Elapsed: {elapsed}s",
        "",
    ]
    for idx, item in enumerate(all_items, 1):
        pub = f" | Publisher: {item['publisher']}" if item.get("publisher") and item["publisher"] != item["source"] else ""
        md_lines.append(
            f"{idx}. **[{item['source']} | {item['category']}{pub}]** {item['title']}\n"
            f"   - URL: {item['url']}\n"
            f"   - Time: {item['published']} | Summary: {item['summary']}"
        )
        if item.get("paywall"):
            md_lines.append("   - Paywall: 需付费订阅才能看全文")
        if item.get("rank"):
            md_lines.append(f"   - Rank: {item['source']} #{item['rank']}")
        if item.get("heat_text"):
            md_lines.append(f"   - Heat: {item['heat_text']}")
        if item.get("lead"):
            md_lines.append(f"   - Article lead: {item['lead']}")

    md_path = os.path.join(OUTPUT_DIR, f"{bot_name}_brief.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines) + "\n")

    print(f"[{bot_name.upper()}] Done in {elapsed}s | Sources: {payload['sources_succeeded']}/{payload['sources_total']} | Items: {payload['total_items']} | Brief size: {os.path.getsize(md_path)} bytes")
    return payload


import fcntl

def main():
    lock_file = open("/tmp/daily_fetch.lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="Daily 0-Token Pre-Fetcher for 6 Bots")
    parser.add_argument("--bot", type=str, default="all", choices=["tech", "finance", "politics", "music", "commerce", "reddit", "events", "releases", "chiangmai", "all"])
    args = parser.parse_args()
    global SUMMARY_LEN
    if args.bot not in ("chiangmai", "events", "all"):
        SUMMARY_LEN = 450  # 摘要加长，AI 才写得出 3～5 句介绍

    runners = {
        "tech": run_tech_bot,
        "finance": run_finance_bot,
        "politics": run_politics_bot,
        "music": run_music_bot,
        "commerce": run_commerce_bot,
        "reddit": run_reddit_bot,
        "events": run_events_bot,
        "releases": run_releases_bot,
        "chiangmai": run_chiangmai_bot,
    }

    if args.bot == "all":
        summary = {}
        for b in ["tech", "finance", "politics", "music", "commerce", "reddit", "events", "releases", "chiangmai"]:
            summary[b] = runners[b]()
    else:
        runners[args.bot]()


if __name__ == "__main__":
    main()
