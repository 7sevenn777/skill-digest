# skill-digest

每天自动生成一份中文 AI 日报并推送到你的 Telegram（同时开 GitHub Issue 归档）：

- **一、值得关注的项目**：AI agent skill / MCP / workflow 相关，按 **star 增速**筛选
  （不看项目新旧，老项目翻红、新项目爆发都能被抓到），入选项目自动抓取 README，
  由大模型（MiMo 等免费 OpenAI 兼容接口）总结"具体是做什么的"。
- **二、AI / 科技热点**：Hacker News 近 48 小时高分新闻的中文快报。

英文内容会转写成中文并保留英文原文对照（标题中英并列、命令名/专有名词保留英文），
其他语言默认直接译成中文。已推送过的内容不会重复：报过的条目不再报，仓库只有
继续大涨（star 增量超上次 30%）才会再次出现。

## 筛选逻辑：看 star 增速，不是看新旧

1. **广撒网拉候选池**：GitHub 搜索两类种子——最近 7 天仍有提交的老仓库（star ≥ 20，
   可能翻红）+ 最近 7 天新建的仓库（star ≥ 5，可能爆发），加上 HN 高分条目。
2. **按 star 增速实测**：用 GitHub GraphQL 逐个统计"最近 7 天新增 star 数"，
   低于 `MIN_WEEKLY_STARS` 的直接淘汰。
3. **README 深读 + 模型二次筛选**：入选仓库抓取 README 喂给模型，输出"是什么 /
   具体做什么 / 链接与热度"，同时扔掉无关噪音、按值得关注程度排序。
   项目标注【新晋】（60 天内创建）或【翻红】（老项目爆火）。
4. **热点新闻版块**：HN 近 `NEWS_DAYS` 天高分故事单独总结，与项目版块自动去重。
5. **推送**：日报写入 `digest/`，转成 Telegram HTML 消息（超长自动分段）推送，
   并开一个 Issue 归档。

## 你需要做的（一次性 5 分钟）

1. **建 Telegram 机器人**：Telegram 里找 [@BotFather](https://t.me/BotFather)，
   发送 `/newbot`，按提示起名，最后会给你一串 **bot token**（形如
   `123456789:AAHxxxxxxxx`），复制保存。

2. **拿你自己的 chat id**：先给你的新 bot 随便发一条消息（必须发，否则它无法主动
   私聊你），然后浏览器打开
   `https://api.telegram.org/bot<你的token>/getUpdates`
   （把 `<你的token>` 换成上一步的 token），在返回的 JSON 里找
   `"chat":{"id":123456789,...}`，这个数字就是 chat id。
   （也可以直接给 [@userinfobot](https://t.me/userinfobot) 发消息获取。）

3. **建 GitHub 仓库**，把本项目所有文件推上去（公开仓库即可，Actions 免费额度足够）。

4. **配 5 个 Secret**：仓库 `Settings → Secrets and variables → Actions → New repository secret`：

   | Secret 名 | 值 |
   |---|---|
   | `LLM_BASE_URL` | 模型接口地址，填到 `/v1` 结尾（OpenAI 兼容格式） |
   | `LLM_API_KEY` | 对应 key |
   | `LLM_MODEL` | 模型名（如 `mimo-v2.5`，以提供方为准） |
   | `TG_BOT_TOKEN` | 第 1 步的 bot token |
   | `TG_CHAT_ID` | 第 2 步的 chat id |

   GitHub 检测用的是 Actions 自带的 `GITHUB_TOKEN`，不用自己建。

5. **手动验证一次**：仓库 Actions 页面 → digest → Run workflow。
   Telegram 收到日报即部署完成，之后每天北京时间 10:23 自动推送。

> 提示：GitHub Actions 的服务器在海外，访问 Telegram API 不需要代理；
> 但你自己在本地测试时，需要能直连 `api.telegram.org`。
> 不想要 Issue 邮件提醒的话，删掉 `digest.yml` 里的 `issues: write` 权限和
> `scripts/digest.py` main() 末尾"开 Issue"那段即可。

## 可调参数（都在 `scripts/digest.py` 顶部）

| 常量 | 含义 |
|---|---|
| `GH_QUERIES` / `HN_QUERIES` | 项目跟踪的关键词 |
| `MIN_WEEKLY_STARS` | 一周新增 star 门槛（默认 10），嫌吵就调高 |
| `MIN_POINTS` | HN 项目条目最低分数门槛 |
| `NEWS_DAYS` / `NEWS_MIN_POINTS` / `NEWS_MAX` | 热点新闻的回看窗口 / 最低分 / 最多条数 |
| `README_CHARS` | 每个仓库喂给模型的 README 字符数上限 |
| `NEW_REPO_DAYS` | 多少天内的仓库算"新晋"，其余标"翻红" |
| `MAX_ITEMS` | 每期项目最多条数 |

## 本地测试

```bash
pip install -r requirements.txt
export GH_TOKEN=...                                  # GitHub token（GraphQL + README 抓取必需）
export LLM_BASE_URL=... LLM_API_KEY=... LLM_MODEL=...
export TG_BOT_TOKEN=... TG_CHAT_ID=...               # 可选
python scripts/digest.py
```

不配 `GH_TOKEN` 只跑 HN 部分；不配 LLM 变量则不过滤、直接输出原文条目；
不配 TG 变量则跳过 Telegram 推送（日报文件照常生成）。
