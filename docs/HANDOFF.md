# 黎明简报 · 交接（2026-11-25）

## 现状
- 网站：https://niukutzu.github.io/daily-fetch/ 。推送到 main 且改了 `web/**` 或 `build_site.py` 会自动重新发布；只想重建网站就手动运行 workflow `daily.yml`，选 bot=「只更新网站」。
- 本地构建需要 `pip install -r requirements.txt`（有 markdown 等），然后 `python3 build_site.py` 生成 `site/`。

## 最近做完
- 11-24 左栏搜索升级（提交 570a415）：全文索引 `site/search.json`、多关键词、不分大小写、结果直达并高亮。详见 `docs/TASK-search.md` 的「完成记录」。
- 11-24 搜索框提示改为「搜索全部情报，空格隔开多个词」。
- 11-25 9 个 Bot 换新头像：图片存在仓库 Variables 的 `BOT_PROFILES`（每个 Bot 的 `img` 是 128px webp data URI，`color` 是主色），不在代码里。

## 待办 / 注意
- 搜索：非结构化报告的“找段落并高亮”还没在真实页面测过（目前所有报告都是结构化的）；搜 `amp`、`lt` 这类词时高亮可能把 `&` 显示异常。
- 派活写代码照 `~/.claude/派活规则.md`。Grok 无人值守时，只要有一个操作没被放行就会整单中止，权限要一次给全。
