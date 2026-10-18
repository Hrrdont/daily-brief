"""Build the daily brief: collect, rank, render markdown + data.json.

Runs inside GitHub Actions (or locally). No third-party deps.

Environment:
    SERVERCHAN_KEY   optional — if set, the brief is pushed to WeChat
    DEEPSEEK_API_KEY optional — if set, an LLM picks and explains the items
    BRIEF_CITY       optional — overrides config.json city
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
TZ = timezone(timedelta(hours=8))
NOW = datetime.now(TZ)
TODAY = NOW.strftime("%Y-%m-%d")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) daily-brief/1.0"}


# ---------------------------------------------------------------- http helpers
def get(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def get_json(url: str, timeout: int = 25):
    return json.loads(get(url, timeout))


def post_json(url: str, payload: dict, timeout: int = 25) -> str:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={**UA, "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def safe(fn, fallback, label):
    try:
        return fn()
    except Exception as exc:  # one dead source must not sink the brief
        print(f"[warn] {label}: {type(exc).__name__} {exc}", file=sys.stderr)
        return fallback


# ---------------------------------------------------------------- collectors
def bilibili_ranking(limit: int = 60) -> list[dict]:
    data = get_json("https://api.bilibili.com/x/web-interface/ranking/v2?rid=0&type=all")
    items = (data.get("data") or {}).get("list") or []
    out = []
    for it in items[:limit]:
        out.append({
            "title": it.get("title", ""),
            "author": (it.get("owner") or {}).get("name", ""),
            "views": (it.get("stat") or {}).get("view", 0),
            "url": it.get("short_link_v2") or f"https://www.bilibili.com/video/{it.get('bvid','')}",
            "desc": (it.get("desc") or "")[:120],
        })
    return out


def hacker_news(limit: int = 5) -> list[dict]:
    ids = get_json("https://hacker-news.firebaseio.com/v0/topstories.json")[: limit * 3]
    out = []
    for sid in ids:
        if len(out) >= limit:
            break
        d = get_json(f"https://hacker-news.firebaseio.com/v0/item/{sid}.json")
        if not d or d.get("type") != "story" or not d.get("title"):
            continue
        url = d.get("url") or f"https://news.ycombinator.com/item?id={sid}"
        out.append({"title": d["title"], "url": url, "score": d.get("score", 0),
                    "domain": urllib.parse.urlparse(url).netloc.replace("www.", "")})
    return out


def rss_items(url: str, limit: int = 3) -> list[dict]:
    raw = get(url)
    root = ET.fromstring(raw)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for entry in root.findall(".//item")[:limit]:
        out.append({
            "title": (entry.findtext("title") or "").strip(),
            "url": (entry.findtext("link") or "").strip(),
            "summary": re.sub(r"<[^>]+>", "", entry.findtext("description") or "")[:160].strip(),
        })
    if out:
        return out
    for entry in root.findall(".//a:entry", ns)[:limit]:
        link = entry.find("a:link", ns)
        out.append({
            "title": (entry.findtext("a:title", default="", namespaces=ns) or "").strip(),
            "url": link.get("href") if link is not None else "",
            "summary": re.sub(r"<[^>]+>", "", entry.findtext("a:summary", default="", namespaces=ns) or "")[:160].strip(),
        })
    return out


def bing_image() -> dict:
    data = get_json("https://www.bing.com/HPImageArchive.aspx?format=js&idx=0&n=1&mkt=zh-CN")
    img = (data.get("images") or [{}])[0]
    return {
        "title": img.get("title", ""),
        "copyright": img.get("copyright", ""),
        "url": "https://www.bing.com" + (img.get("url") or ""),
        "page": "https://www.bing.com",
    }


WMO = {
    0: "晴", 1: "大部晴", 2: "多云", 3: "阴", 45: "雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨", 61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨", 71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨", 81: "强阵雨", 82: "暴阵雨", 85: "阵雪", 86: "强阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷暴伴冰雹",
}


def weather(city: str) -> dict | None:
    geo = get_json("https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode(
        {"name": city, "count": 1, "language": "zh", "format": "json"}))
    hits = geo.get("results") or []
    if not hits:
        return None
    g = hits[0]
    fc = get_json("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
        "latitude": g["latitude"], "longitude": g["longitude"],
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weathercode",
        "timezone": "Asia/Shanghai", "forecast_days": 1,
    }))
    d = fc.get("daily") or {}
    code = (d.get("weathercode") or [0])[0]
    return {
        "city": g.get("name") or city,
        "region": ", ".join(x for x in [g.get("admin1"), g.get("country")] if x),
        "desc": WMO.get(int(code), "未知"),
        "tmax": round((d.get("temperature_2m_max") or [0])[0]),
        "tmin": round((d.get("temperature_2m_min") or [0])[0]),
        "rain": (d.get("precipitation_probability_max") or [0])[0] or 0,
    }


# ---------------------------------------------------------------- ranking
def score_bili(item: dict) -> int:
    text = (item["title"] + " " + item["author"] + " " + item.get("desc", "")).lower()
    if any(bad.lower() in text for bad in CONFIG["bilibili_exclude"]):
        return -1
    score = 0
    for kw, w in CONFIG["bilibili_weights"].items():
        if kw.lower() in text:
            score += w
    score += min(item["views"] // 500000, 8)  # popularity, capped
    return score


def pick_bili(items: list[dict], n: int = 3) -> list[dict]:
    scored = [(score_bili(i), i) for i in items]
    scored = [x for x in scored if x[0] >= CONFIG["bilibili_min_score"]]
    scored.sort(key=lambda x: -x[0])
    picked, seen_words = [], set()
    for _, item in scored:
        key = set(re.findall(r"[\u4e00-\u9fff]{2}", item["title"]))
        if seen_words and len(key & seen_words) >= 2:
            continue
        seen_words |= key
        picked.append(item)
        if len(picked) >= n:
            break
    return picked


# ---------------------------------------------------------------- optional LLM
def llm_digest(payload: dict) -> dict | None:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        return None
    prompt = (
        "你是我的编辑。下面是今天抓到的候选内容，请挑出真正值得我花时间的东西，写成一份中文早报。\n"
        "我的偏好：技术/AI/编程、物理与数学、开源与硬件折腾、自我成长（要能验证的，不要鸡汤）、"
        "音乐与美学。讨厌：明星八卦、标题党、美妆穿搭、空泛政论、营销号。\n"
        "要求：热点 2-3 条（必须有信息量，不要纯娱乐）；成长 2 条；每条给一句『为什么值得你看』，"
        "要具体、说人话、不超过 40 字。不要编造事实，只能基于给定标题和描述。\n"
        "只输出 JSON：{\"hot\":[{\"title\":\"\",\"url\":\"\",\"why\":\"\"}],"
        "\"growth\":[{\"title\":\"\",\"url\":\"\",\"why\":\"\"}],\"closing\":\"一句话总结\"}\n\n"
        + json.dumps(payload, ensure_ascii=False)[:12000]
    )
    try:
        resp = post_json("https://api.deepseek.com/chat/completions", {
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.6,
            "response_format": {"type": "json_object"},
        }, timeout=90)
        content = json.loads(resp)["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception as exc:
        print(f"[warn] llm: {type(exc).__name__} {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------- render
def plain_digest(ctx: dict) -> dict:
    hot = []
    for item in ctx["bili"][:2]:
        hot.append({"title": item["title"], "url": item["url"],
                    "why": f"{item['author']} · {item['views'] / 10000:.0f} 万播放，命中你在意的技术/知识方向"})
    for item in ctx["hn"][:2]:
        hot.append({"title": item["title"], "url": item["url"],
                    "why": f"Hacker News {item['score']} 分 · {item['domain']}"})

    growth = []
    for item in ctx["ruanyf"][:1]:
        growth.append({"title": item["title"], "url": item["url"], "why": "阮一峰周刊：偏工程与视野，适合周末慢读"})
    for item in ctx["sspai"][:1]:
        growth.append({"title": item["title"], "url": item["url"], "why": "少数派：具体可操作的效率/生活议题"})

    closing = ""
    if hot:
        closing = f"今天只看一条的话：{hot[0]['title']}"
    return {"hot": hot, "growth": growth, "closing": closing}


def render_markdown(brief: dict) -> str:
    lines = [f"## ☀️ 早报 · {NOW.strftime('%m月%d日')}", ""]
    w = brief.get("weather")
    if w:
        lines.append(f"**{w['city']}** {w['desc']} {w['tmin']}~{w['tmax']}°C · 降水 {w['rain']}%")
        lines.append("")

    if brief["hot"]:
        lines.append("### 🔥 值得看一眼")
        for i, item in enumerate(brief["hot"], 1):
            lines.append(f"{i}. **[{item['title']}]({item['url']})**")
            lines.append(f"   {item['why']}")
        lines.append("")

    if brief["growth"]:
        lines.append("### 🌱 慢一点的东西")
        for i, item in enumerate(brief["growth"], 1):
            lines.append(f"{i}. **[{item['title']}]({item['url']})**")
            lines.append(f"   {item['why']}")
        lines.append("")

    img = brief.get("image")
    if img:
        lines.append("### 🖼 今天的一张图")
        lines.append(f"![今日图]({img['url']})")
        lines.append(img.get("copyright", ""))
        lines.append("")

    if brief.get("closing"):
        lines.append("---")
        lines.append(f"> {brief['closing']}")
    return "\n".join(lines)


def push_serverchan(title: str, markdown: str) -> None:
    key = os.environ.get("SERVERCHAN_KEY", "").strip()
    if not key:
        print("[info] SERVERCHAN_KEY not set, skip push")
        return
    url = f"https://sctapi.ftqq.com/{key}.send"
    body = urllib.parse.urlencode({"title": title, "desp": markdown}).encode()
    req = urllib.request.Request(url, data=body, headers=UA, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        print("[info] serverchan:", r.read().decode("utf-8", "replace")[:200])


# ---------------------------------------------------------------- main
def main() -> None:
    city = os.environ.get("BRIEF_CITY") or CONFIG.get("city", "上海")
    ctx = {
        "bili": safe(bilibili_ranking, [], "bilibili"),
        "hn": safe(hacker_news, [], "hackernews"),
        "ruanyf": safe(lambda: rss_items(CONFIG["feeds"]["ruanyf"], 2), [], "ruanyf"),
        "sspai": safe(lambda: rss_items(CONFIG["feeds"]["sspai"], 2), [], "sspai"),
        "weather": safe(lambda: weather(city), None, "weather"),
        "image": safe(bing_image, None, "bing"),
    }
    ctx["bili"] = pick_bili(ctx["bili"])

    payload = {
        "bili": [{"title": i["title"], "author": i["author"], "views": i["views"], "url": i["url"]} for i in ctx["bili"]],
        "hn": ctx["hn"],
        "ruanyf": ctx["ruanyf"],
        "sspai": ctx["sspai"],
    }
    brief = llm_digest(payload) or plain_digest(ctx)

    brief["date"] = TODAY
    brief["generated_at"] = NOW.isoformat(timespec="seconds")
    brief["weather"] = ctx["weather"]
    brief["image"] = ctx["image"]
    brief.setdefault("hot", [])
    brief.setdefault("growth", [])

    md = render_markdown(brief)

    (ROOT / "data.json").write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")

    hist_path = ROOT / "history.json"
    hist = []
    if hist_path.exists():
        try:
            hist = json.loads(hist_path.read_text(encoding="utf-8"))
        except Exception:
            hist = []
    hist = [h for h in hist if h.get("date") != TODAY]
    hist.append(brief)
    hist = sorted(hist, key=lambda h: h.get("date", ""))[-30:]
    hist_path.write_text(json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")

    print(md)
    title = f"{NOW.strftime('%m/%d')} 早报 · " + (brief["hot"][0]["title"][:28] if brief["hot"] else "今日摘要")
    safe(lambda: push_serverchan(title, md), None, "serverchan")


if __name__ == "__main__":
    main()
