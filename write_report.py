#!/usr/bin/env python3
"""
把抓取结果交给 AI，写成中文日报，并推送。

AI 接口用 OpenAI 兼容格式，3 个值都从环境变量（GitHub Secrets）读取：
  LLM_BASE_URL   接口地址，例如 https://openrouter.ai/api/v1
  LLM_API_KEY    密钥
  LLM_MODEL      默认模型，例如 google/gemini-3.1-flash-lite
单个 Bot 想用别的模型：再加一个 LLM_MODEL_<BOT>，例如 LLM_MODEL_POLITICS

推送：
  一定会：保存到 reports/<bot>/YYYY-MM-DD.md，并在仓库里开一个 Issue（GitHub App 会通知手机）
  可选：  填了 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID 就同时发到 Telegram
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import rich_report

BASE = os.path.dirname(os.path.abspath(__file__))
CST = timezone(timedelta(hours=8))

NAMES = {
    "tech": "全球科技每日精选",
    "finance": "全球金融每日精选",
    "politics": "国际政治每日精选",
    "music": "电音与 DJ 每日精选",
    "commerce": "海外商业与套利每日精选",
    "reddit": "Reddit 每日精选",
    "events": "演出与艺文每周排期",
    "releases": "每周六全球音乐发行盘点",
    "chiangmai": "清迈未来 7 天活动",
}

COMMON_RULES = """
【通用规则】
- 读者是中文用户，不懂英文。全部用平实、句子短的简体中文。
- 只能使用下面「抓取结果」里的内容，不要编造任何新闻、数字、链接。抓取结果里没有的就不写。
- 每条都要附原文链接（用抓取结果里的 URL 原样照抄），格式：[中文标题](URL)。
- 专业术语第一次出现时写成：English (中文解释)。
- 不用营销号、网络流行烂梗（如：炸裂、王炸、殿堂级、白嫖、硬核、遥遥领先）。
- 原文自称的收入、用户数、价格等数字，标注【自称，未验证】。
- 输出 Markdown。不要写开场白和结束语，直接从第一个标题开始。
"""


def call_llm(bot: str, system: str, user: str, json_mode: bool = False) -> str:
    base = os.environ["LLM_BASE_URL"].rstrip("/")
    key = os.environ["LLM_API_KEY"]
    model = os.environ.get(f"LLM_MODEL_{bot.upper()}") or os.environ.get("LLM_MODEL")
    if not model:
        sys.exit("[出错] 没有读到模型名。请到仓库 Settings → Secrets and variables → Actions → Variables 标签页，新建名为 LLM_MODEL 的变量。")
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.3,
    }
    if os.environ.get("LLM_MAX_TOKENS"):
        payload["max_tokens"] = int(os.environ["LLM_MAX_TOKENS"])
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    def send(body):
        req = urllib.request.Request(
            base + "/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read())

    try:
        try:
            data = send(payload)
        except urllib.error.HTTPError as e:
            if not (json_mode and e.code in (400, 422)):
                raise
            print(f"[LLM] 接口不支持 JSON 模式（HTTP {e.code}），改用普通模式重试")
            payload.pop("response_format", None)
            data = send(payload)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:800]
        print(f"[LLM 出错] HTTP {e.code}，接口返回：{detail}")
        print(f"[提示] 请求地址结尾是 /chat/completions，地址共 {len(base)} 个字符，模型 = {model}")
        raise
    usage = data.get("usage", {})
    choice = data["choices"][0]
    print(f"[LLM] model={model} prompt_tokens={usage.get('prompt_tokens')} completion_tokens={usage.get('completion_tokens')} finish={choice.get('finish_reason')}")
    return (choice["message"].get("content") or "").strip()


def post_json(url, payload, headers):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")


def open_issue(title, body):
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not (token and repo):
        return
    post_json(
        f"https://api.github.com/repos/{repo}/issues",
        {"title": title, "body": body[:65000], "labels": ["日报"]},
        {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )


def send_telegram(text):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        return
    for i in range(0, len(text), 3900):  # Telegram 单条上限 4096 字
        post_json(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            {"chat_id": chat, "text": text[i:i + 3900], "disable_web_page_preview": True},
            {"Content-Type": "application/json"},
        )


def main():
    bot = sys.argv[1]
    today = datetime.now(CST).strftime("%Y-%m-%d")
    title = f"{NAMES.get(bot, bot)} · {today}"
    brief = open(os.path.join(BASE, "output", f"{bot}_brief.md"), encoding="utf-8").read()
    prompt = open(os.path.join(BASE, "prompts", f"{bot}.md"), encoding="utf-8").read()
    user = f"今天是北京时间 {today}。\n\n=== 抓取结果 ===\n{brief}"
    out_dir = os.path.join(BASE, "reports", bot)
    os.makedirs(out_dir, exist_ok=True)

    rich = None
    if bot in rich_report.JSON_BOTS:
        try:
            fetched = json.load(open(os.path.join(BASE, "output", f"{bot}_latest.json"), encoding="utf-8"))
            rich = rich_report.build(bot, today, title, prompt, user, fetched,
                                     lambda sysmsg, usermsg: call_llm(bot, sysmsg, usermsg, json_mode=True))
        except Exception as e:
            print(f"[WARN] 结构化报告失败，改用普通 Markdown 报告：{e}")
            rich = None

    if rich:
        with open(os.path.join(out_dir, f"{today}.json"), "w", encoding="utf-8") as f:
            json.dump(rich, f, ensure_ascii=False, indent=1)
        report = rich_report.to_markdown(rich)
        short = rich_report.to_short(rich)
    else:
        sys_prompt = (prompt + "\n" + rich_report.FALLBACK_FORMAT) if bot in rich_report.JSON_BOTS else prompt
        report = call_llm(bot, sys_prompt + "\n" + COMMON_RULES, user)
        short = None
        stale = os.path.join(out_dir, f"{today}.json")
        if os.path.exists(stale):
            os.remove(stale)
    full = f"# {title}\n\n{report}\n"
    with open(os.path.join(out_dir, f"{today}.md"), "w", encoding="utf-8") as f:
        f.write(full)

    site = os.environ.get("SITE_URL", "https://niukutzu.github.io/daily-fetch").rstrip("/")
    tg = f"{title}\n\n{short}\n\n完整报告：{site}/#{bot}-{today}" if short else full
    for fn, arg in ((open_issue, (title, report)), (send_telegram, (tg,))):
        try:
            fn(*arg)
        except Exception as e:
            print(f"[WARN] 推送失败 {fn.__name__}: {e}")
    print(f"[DONE] {title}" + (f"（结构化：{len(rich['items'])} 条精选）" if rich else ""))


if __name__ == "__main__":
    main()
