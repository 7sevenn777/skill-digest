#!/usr/bin/env python3
"""
每天从 GitHub / Hacker News 发现 AI agent skill / MCP / workflow 相关的好项目，
总结近两天的 AI / 科技热点新闻，整理成中文日报推送到 Telegram（同时开 Issue 归档）。

热度口径是“star 增长速度”而不是“是否新仓库”：
  1. 广撒网拉候选池（最近仍活跃的老仓库 + 刚出生的新仓库）
  2. 用 GitHub GraphQL 逐个统计“最近 7 天新增 star 数”，达标才入选
     —— 老项目因作者更新爆火（翻红）和新项目爆发（新晋）都能被发现
  3. 对入选仓库抓取 README，交给 OpenAI 兼容接口（MiMo 等免费接口均可）：
     过滤无关噪音，按 README 写出“具体是做什么的”深度介绍；
     英文内容转写成中文（保留命令名/专有名词原文），标题给中英对照，
     其他语言默认译成中文
  4. 另取 HN 近几天高分故事，总结“AI / 科技热点”版块
  5. 写入 digest/，推送到 Telegram（自动分段），并在 GitHub Actions 中开 Issue 归档

已推送过的内容不会重复：HN/新闻报过不再报；GitHub 仓库只有当 star 增量
超过上次的 30% 才会再次出现（持续爆火的项目会被连续跟踪）。

本地测试：
    export GH_TOKEN=...                # GitHub token（GraphQL 和 README 抓取需要）
    export LLM_BASE_URL=... LLM_API_KEY=... LLM_MODEL=...
    export TG_BOT_TOKEN=... TG_CHAT_ID=...   # 可选，不配则跳过 Telegram 推送
    python scripts/digest.py
"""

import html
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ---------------- 配置：想跟踪什么，改这里 ----------------
HN_QUERIES = ["MCP", "Claude skills", "agent skills", "n8n"]          # HN 关键词
GH_QUERIES = ["MCP server", "agent skills", "claude skills", "AI agent", "n8n workflow"]
SEED_PER_QUERY = 15      # 每个关键词取多少候选仓库
MIN_WEEKLY_STARS = 10    # 一周内新增 star 少于此数的仓库不入选
MIN_POINTS = 30          # HN 条目最低热度（skill 关键词命中）
MAX_ITEMS = 15           # 每期项目最多条数
NEW_REPO_DAYS = 60       # 仓库年龄小于此值标“新晋”，否则标“翻红”
DAYS = 7                 # 项目回看窗口

NEWS_DAYS = 2            # 热点新闻回看窗口（周一早上跑，覆盖周末新闻）
NEWS_MIN_POINTS = 100    # 热点新闻最低 HN 分数
NEWS_MAX = 10            # 热点新闻最多条数
README_CHARS = 4000      # 每个 README 最多取多少字符喂给模型
# -----------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
DIGEST_DIR = ROOT / "digest"
SEEN_FILE = ROOT / "data" / "seen.json"

SINCE = datetime.now(timezone.utc) - timedelta(days=DAYS)

GQL_URL = "https://api.github.com/graphql"
GQL_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    nameWithOwner
    description
    url
    createdAt
    stargazerCount
    stargazers(first: 100, orderBy: {field: STARRED_AT, direction: DESC}) {
      edges { starredAt }
    }
  }
}"""


def log(msg):
    print(msg, flush=True)


def fetch_hn():
    """Hacker News 最近一周、命中 skill 关键词的高分条目（Algolia API，无需鉴权）"""
    items = []
    for q in HN_QUERIES:
        try:
            r = requests.get(
                "https://hn.algolia.com/api/v1/search_by_date",
                params={
                    "query": q,
                    "tags": "story",
                    "numericFilters": (
                        f"created_at_i>{int(SINCE.timestamp())},points>{MIN_POINTS}"
                    ),
                },
                timeout=30,
            )
            r.raise_for_status()
            for hit in r.json().get("hits", []):
                if not hit.get("title"):
                    continue
                items.append(
                    {
                        "id": f"hn:{hit['objectID']}",
                        "src": "HN",
                        "title": hit["title"],
                        "desc": "",
                        "url": hit.get("url")
                        or f"https://news.ycombinator.com/item?id={hit['objectID']}",
                        "score": hit.get("points") or 0,
                        "gain": hit.get("points") or 0,
                    }
                )
        except Exception as e:
            log(f"[warn] HN 查询 {q!r} 失败: {e}")
    return items


def fetch_news():
    """HN 近几天高分故事（不限关键词），作为 AI/科技热点素材"""
    since = datetime.now(timezone.utc) - timedelta(days=NEWS_DAYS)
    try:
        r = requests.get(
            "https://hn.algolia.com/api/v1/search_by_date",
            params={
                "tags": "story",
                "numericFilters": (
                    f"created_at_i>{int(since.timestamp())},points>{NEWS_MIN_POINTS}"
                ),
                "hitsPerPage": 50,
            },
            timeout=30,
        )
        r.raise_for_status()
        hits = [h for h in r.json().get("hits", []) if h.get("title")]
        hits.sort(key=lambda h: -(h.get("points") or 0))
        return [
            {
                "id": f"news:{h['objectID']}",
                "title": h["title"],
                "url": h.get("url")
                or f"https://news.ycombinator.com/item?id={h['objectID']}",
                "score": h.get("points") or 0,
            }
            for h in hits[:NEWS_MAX]
        ]
    except Exception as e:
        log(f"[warn] HN 热点抓取失败: {e}")
        return []


def search_candidates(token):
    """拉候选池：最近仍活跃的老仓库（可能翻红）+ 刚出生的新仓库（可能爆发）"""
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    filters = [
        # 老项目：最近一周有提交、有一定 star 基数
        f"pushed:>={SINCE.date().isoformat()} stars:>=20",
        # 新项目：本周创建、star 还不多但已经有人关注
        f"created:>={SINCE.date().isoformat()} stars:>=5",
    ]
    seeds, seen_names = [], set()
    for f in filters:
        for q in GH_QUERIES:
            try:
                r = requests.get(
                    "https://api.github.com/search/repositories",
                    params={
                        "q": f"{q} {f}",
                        "sort": "stars",
                        "order": "desc",
                        "per_page": SEED_PER_QUERY,
                    },
                    headers=headers,
                    timeout=30,
                )
                r.raise_for_status()
                for repo in r.json().get("items", []):
                    if repo["full_name"] not in seen_names:
                        seen_names.add(repo["full_name"])
                        seeds.append(repo["full_name"])
            except Exception as e:
                log(f"[warn] GitHub 搜索 {q!r} 失败: {e}")
    return seeds


def measure_velocity(candidates, token):
    """逐个统计候选仓库最近一周新增 star 数，只保留达标的"""
    items = []
    for full_name in candidates:
        owner, name = full_name.split("/", 1)
        try:
            r = requests.post(
                GQL_URL,
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "query": GQL_QUERY,
                    "variables": {"owner": owner, "name": name},
                },
                timeout=30,
            )
            r.raise_for_status()
            repo = r.json()["data"]["repository"]
            if repo is None:
                continue
            # stargazers 按 STARRED_AT 倒序，第一页就是最近的 star 时间
            gained = 0
            for edge in repo["stargazers"]["edges"]:
                t = datetime.fromisoformat(edge["starredAt"].replace("Z", "+00:00"))
                if t >= SINCE:
                    gained += 1
                else:
                    break
            if gained < MIN_WEEKLY_STARS:
                continue
            created = datetime.fromisoformat(repo["createdAt"].replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - created).days
            items.append(
                {
                    "id": f"gh:{full_name}",
                    "src": "GitHub",
                    "title": repo["nameWithOwner"],
                    "desc": (repo.get("description") or "")[:300],
                    "url": repo["url"],
                    "score": repo["stargazerCount"],
                    "gain": gained,
                    "age": age_days,
                }
            )
        except Exception as e:
            log(f"[warn] 查询 {full_name} star 增速失败: {e}")
    items.sort(key=lambda x: -x["gain"])
    return items[:40]


def fetch_readme(full_name, token):
    """抓仓库 README 原文（截断），供模型总结“具体是做什么的”"""
    try:
        r = requests.get(
            f"https://api.github.com/repos/{full_name}/readme",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.raw",
            },
            timeout=30,
        )
        if r.status_code == 200:
            return r.text[:README_CHARS]
    except Exception as e:
        log(f"[warn] 获取 {full_name} README 失败: {e}")
    return ""


def load_seen():
    """seen.json 记录 {id: 上次推送时的热度}；兼容旧版 list 格式"""
    if SEEN_FILE.exists():
        try:
            data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return {k: 0 for k in data}
            return data
        except Exception:
            return {}
    return {}


def pick_new(items, seen):
    """去重 + 重复抑制：HN 报过不再报；GitHub 仓库只有继续大涨（>30%）才再报"""
    fresh, urls = [], set()
    for it in sorted(items, key=lambda x: -x["gain"]):
        if it["url"] in urls:
            continue
        urls.add(it["url"])
        old = seen.get(it["id"])
        if it["src"] == "HN" and old is not None:
            continue
        if old is not None and it["gain"] <= old * 1.3:
            continue
        fresh.append(it)
    return fresh[:MAX_ITEMS]


def repos_raw(items):
    """把入选项目（含 README 节选）拼成喂给模型的原料"""
    parts = []
    for it in items:
        if it["src"] == "GitHub":
            tag = "新晋" if it["age"] < NEW_REPO_DAYS else "翻红"
            head = (
                f"- [GitHub·{tag}] {it['title']} — 本周 +{it['gain']} star"
                f"（总 {it['score']}，创建 {it['age']} 天）— {it['url']}"
            )
        else:
            head = f"- [HN] {it['title']} — {it['score']} 分 — {it['url']}"
        body = ""
        if it["desc"]:
            body += f"\n  简介：{it['desc']}"
        if it.get("readme"):
            body += f"\n\n  ===== README 节选（{it['title']}）=====\n{it['readme']}\n  ===== 节选结束 ====="
        parts.append(head + body)
    return "\n\n".join(parts)


def news_raw(items):
    return "\n".join(f"- {n['title']} — {n['score']} 分 — {n['url']}" for n in items)


def llm_call(system, user):
    """调用 OpenAI 兼容接口；3 次重试，全部失败返回 None"""
    base = os.environ.get("LLM_BASE_URL", "").rstrip("/")
    key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "")
    if not (base and key and model):
        log("[warn] 未配置 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL，跳过模型整理")
        return None

    url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
    payload = {
        "model": model,
        "temperature": 0.3,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    for attempt in range(3):
        try:
            r = requests.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {key}"},
                timeout=180,
            )
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"].strip()
            if text:
                return text
        except Exception as e:
            log(f"[warn] 模型调用第 {attempt + 1} 次失败: {e}")
            time.sleep(3 * (attempt + 1))
    return None


def curate_repos(items):
    """项目版块：过滤噪音 + 基于 README 总结“具体是做什么的”"""
    if not items:
        return "本周没有达标的新项目。"
    text = llm_call(
        "你是一名严格的中文 AI 资讯周报编辑，只保留与 AI agent、skill、MCP、"
        "工作流、开发工具直接相关的条目，输出语言为中文。",
        (
            "以下是本周通过 star 增速筛选出的候选条目（部分附 README 节选）：\n\n"
            f"{repos_raw(items)}\n\n"
            "请整理成周报的“值得关注的项目”版块：\n"
            "1. 先过滤：与 AI agent / skill / MCP / 工作流 / 开发工具无关的条目直接丢弃\n"
            "2. 每个项目输出一段：\n"
            "   ### 中文项目名（English 原名）【新晋/翻红】\n"
            "   - **是什么**：一句话定位\n"
            "   - **具体做什么**：根据 README 用 2-4 个要点说明核心功能和亮点；"
            "README 是英文时转写成中文（命令名、库名、专有名词保留英文原文），"
            "其他语言一律写成中文；没有 README 的条目根据标题和简介简要点评\n"
            "   - **链接与热度**：保留原始链接和 star 数据（如 `本周 +120 star / 总 3.4k`）\n"
            "3. 标题必须中英对照；按值得关注程度从高到低排序；只输出正文，不要开场白和总结\n"
        ),
    )
    return text if text else repos_raw(items)  # 模型失败时降级输出原文


def curate_news(items):
    """热点版块：AI / 科技新闻总结，英文标题给中英对照"""
    if not items:
        return "近几天没有高分热点。"
    text = llm_call(
        "你是一名中文科技媒体编辑，负责把 Hacker News 热门故事整理成简洁的中文热点快报。",
        (
            f"以下是 Hacker News 近 {NEWS_DAYS} 天的热门故事：\n\n"
            f"{news_raw(items)}\n\n"
            "请整理成“AI / 科技热点”版块：\n"
            "- 只保留 AI 或科技相关的条目，其余丢弃\n"
            "- 每条格式：`- **中文标题**（English 原标题）— 一两句话中文摘要`；"
            "标题是英文的必须中文翻译 + 英文原文对照，其他语言直接译成中文不附原文\n"
            f"- 最多 {NEWS_MAX} 条，按热度从高到低排序，保留原始链接，只输出列表\n"
        ),
    )
    return text if text else news_raw(items)


def md_to_html(md_text):
    """把周报的 markdown 转成 Telegram 支持的 HTML 子集"""
    s = html.escape(md_text, quote=False)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r'<a href="\2">\1</a>', s)
    s = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", s)
    s = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", s, flags=re.M)
    s = re.sub(r"^&gt;\s?(.*)$", r"<i>\1</i>", s, flags=re.M)
    s = s.replace("\n- ", "\n• ")
    return s


def chunk_text(text, limit=3900):
    """Telegram 单条消息上限 4096 字符，按段落切块"""
    parts, cur = [], ""
    for para in text.split("\n\n"):
        while len(para) > limit:
            parts.append(para[:limit])
            para = para[limit:]
        if cur and len(cur) + len(para) + 2 > limit:
            parts.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        parts.append(cur)
    return parts


def push_telegram(digest_md):
    """推送到 Telegram；未配置或失败时只记日志，不影响 digest 文件和 Issue"""
    token = os.environ.get("TG_BOT_TOKEN", "")
    chat_id = os.environ.get("TG_CHAT_ID", "")
    if not (token and chat_id):
        log("[info] 未配置 TG_BOT_TOKEN / TG_CHAT_ID，跳过 Telegram 推送")
        return
    parts = chunk_text(md_to_html(digest_md))
    ok = 0
    for i, part in enumerate(parts, 1):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": part,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=30,
            )
            r.raise_for_status()
            ok += 1
        except Exception as e:
            log(f"[warn] Telegram 推送第 {i}/{len(parts)} 段失败: {e}")
            time.sleep(2)
    log(f"Telegram 推送完成：{ok}/{len(parts)} 段")


def main():
    token = os.environ.get("GH_TOKEN")

    log("抓取 Hacker News（skill 关键词）…")
    hn_items = fetch_hn()
    log(f"HN 达标 {len(hn_items)} 条")

    if token:
        log("搜索候选仓库…")
        seeds = search_candidates(token)
        log(f"候选仓库 {len(seeds)} 个，测量 star 增速…")
        gh_items = measure_velocity(seeds, token)
        log(f"一周新增 star ≥ {MIN_WEEKLY_STARS} 的仓库 {len(gh_items)} 个")
    else:
        log("[warn] 未设置 GH_TOKEN，跳过 GitHub star 增速检测（本地测试可只跑 HN）")
        gh_items = []

    seen = load_seen()
    fresh = pick_new(hn_items + gh_items, seen)
    log(f"去重后入选 {len(fresh)} 条")

    # 入选的 GitHub 仓库抓 README，供模型深度总结
    if token:
        for it in fresh:
            if it["src"] == "GitHub":
                it["readme"] = fetch_readme(it["title"], token)

    # 热点新闻：换个关键词维度抓，排除已入选条目和已推送过的新闻
    used_urls = {it["url"] for it in fresh}
    news = [
        n for n in fetch_news()
        if n["url"] not in used_urls and n["id"] not in seen
    ]
    log(f"热点新闻候选 {len(news)} 条")

    if not fresh and not news:
        log("当天没有任何达标的新内容，跳过。")
        return

    date = datetime.now(timezone.utc).date().isoformat()
    digest = (
        f"# AI Skill 日报 · {date}\n\n"
        f"> 自动生成 · 项目口径：一周 star 增速 / HN 热度 · 热点口径：近 {NEWS_DAYS} 天 HN 高分\n\n"
        f"## 一、值得关注的项目\n\n{curate_repos(fresh)}\n\n"
        f"## 二、AI / 科技热点\n\n{curate_news(news)}\n"
    )

    DIGEST_DIR.mkdir(exist_ok=True)
    (DIGEST_DIR / f"{date}.md").write_text(digest, encoding="utf-8")
    log(f"已写入 digest/{date}.md")

    seen.update({it["id"]: it["gain"] for it in fresh})
    seen.update({n["id"]: n["score"] for n in news})
    SEEN_FILE.parent.mkdir(exist_ok=True)
    SEEN_FILE.write_text(
        json.dumps(seen, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    push_telegram(digest)

    # 在 GitHub Actions 里再开一个 Issue 归档（Watch 自己的仓库也会收到邮件提醒）
    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo and token:
        try:
            r = requests.post(
                f"https://api.github.com/repos/{repo}/issues",
                json={"title": f"AI Skill 日报 · {date}", "body": digest},
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            r.raise_for_status()
            log(f"已创建 Issue：{r.json()['html_url']}")
        except Exception as e:
            log(f"[warn] Issue 创建失败（digest 文件已生成）: {e}")

    print("\n" + digest)


if __name__ == "__main__":
    main()
