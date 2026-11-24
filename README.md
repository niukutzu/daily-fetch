# 每日情报（从 Grok Bot 迁移过来）

每天 GitHub 自动免费运行抓取脚本，再调用你填写的 AI 接口写成中文报告。

## 报告在哪里看
- **阅读器网站**：https://niukutzu.github.io/daily-fetch/ （手机、电脑都能看，看过的自动标已读）
- **RSS 订阅**：https://niukutzu.github.io/daily-fetch/feeds/all.xml （单个 Bot 把 all 换成代号，如 tech.xml）
- **reports 文件夹**：所有报告按日期存档，`reports/<bot>/YYYY-MM-DD.md`。

## 运行时间（北京时间，GitHub 经常会晚 5～30 分钟）
| Bot | 代号 | 时间 |
|---|---|---|
| 全球科技 | tech | 每天 08:00 |
| 国际政治 | politics | 每天 08:15 |
| 全球金融 | finance | 每天 08:30 |
| Reddit | reddit | 每天 08:45 |
| 电音与DJ | music | 每天 09:00 |
| 海外商业与套利 | commerce | 每天 09:15 |
| 演出与艺文 | events | 每周四 09:30 |
| 音乐发行 | releases | 每周六 09:30 |

## 换 AI 服务或模型（不用改代码）
仓库 → Settings → Secrets and variables → Actions：
- **Secrets** 标签页：`LLM_BASE_URL`（接口地址）、`LLM_API_KEY`（密钥）
- **Variables** 标签页：`LLM_MODEL`（模型名，不填就用 `google/gemini-3.1-flash-lite`）
- 想让某个 Bot 单独用别的模型：在 Variables 里加 `LLM_MODEL_POLITICS` 这样的名字

接口必须是 OpenAI 兼容格式（OpenRouter、各种中转站、Gemini、OpenAI、xAI 都支持）。

## 手动运行一次
Actions → 每日情报 → Run workflow → 选 Bot → 绿色 Run workflow。

## 想改报告内容
编辑 `prompts/<bot>.md`，就是给 AI 的要求，用中文写。

## 文件说明
- `fetch_all.py`：抓取脚本（原 Grok 云电脑上的版本；Reddit 改用 Arctic Shift 接口）
- `write_report.py`：调用 AI 写报告、推送
- `state/`：演出和音乐发行的去重记录
- `.github/workflows/daily.yml`：定时设置
