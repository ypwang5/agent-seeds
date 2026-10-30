"""Dify 代码节点的源码。每个函数会被 build_dsl.py 原样放进一个代码节点（函数名改成 main）。

约束（Dify 沙箱）：只能用标准库；import 必须写在函数体内；返回 dict，键就是节点的输出变量。
所以每个函数都是自包含的纯函数，可以在本地直接单测（tests/test_code_nodes.py）。
"""


def parse_queries(llm_text: str, topic: str) -> dict:
    """解析"扩展检索词"节点的 JSON；解析失败就退回用原始领域词，不让整个流程断掉。"""
    import json
    import re
    m = re.search(r"\{.*\}", llm_text or "", re.S)
    try:
        data = json.loads(m.group()) if m else {}
    except ValueError:
        data = {}
    en = str(data.get("en") or topic).strip()[:80]
    zh = str(data.get("zh") or topic).strip()[:40]
    return {"en": en, "zh": zh}


def time_window(months) -> dict:
    """时间窗：since = 新帖的起点（默认近 12 个月）；alive = 老帖"还活着"的门槛（近 6 个月内有回复）。
    各来源接口要的格式不同（HN、V2EX 要时间戳，GitHub 要日期），这里一次算好。"""
    import time
    try:
        months = min(max(int(months or 12), 1), 60)
    except (TypeError, ValueError):
        months = 12
    now = int(time.time())
    since_ts = now - months * 30 * 86400
    alive_ts = now - min(months, 6) * 30 * 86400
    day = lambda ts: time.strftime("%Y-%m-%d", time.gmtime(ts))
    return {"months": months, "since_ts": since_ts, "since_date": day(since_ts),
            "alive_ts": alive_ts, "alive_date": day(alive_ts)}


def plan_followups(gh_old: str, v2_old: str, as_search: str, alive_date: str, as_text: list = None) -> dict:
    """第二轮抓取的地址清单（交给迭代节点逐个 GET）：

    - GitHub / V2EX 的老热帖：抓它们的回复，看近期还有没有人在聊（HN 不查：实测 1 年前的高回复帖近半年都没有新评论）；
    - App Store：按领域词搜到的前 3 个 App，抓它们最新的用户评论。
    meta 与 urls 一一对应，记下每个地址是什么、属于哪个帖子 / App。

    as_text：苹果搜索接口的响应头带 content-disposition: attachment，Dify 把它存成文件、body 为空（实测），
    所以先经"文档提取器"读回文本，从这里传进来；body 有内容时优先用 body。"""
    import json

    def load(body):
        try:
            return json.loads(body or "{}")
        except ValueError:
            return {}

    if not (as_search or "").strip() and as_text:
        as_search = as_text[0]
    urls, meta = [], []
    for it in load(gh_old).get("items", [])[:6]:
        if it.get("comments_url"):
            # since 只返回这个时间之后有更新的评论；一条都没有就说明这个老帖已经没人聊了
            urls.append(f"{it['comments_url']}?since={alive_date}T00:00:00Z&per_page=100")
            meta.append({"kind": "gh_old", "title": it.get("title") or "", "url": it.get("html_url") or "",
                         "created": (it.get("created_at") or "")[:10], "heat": int(it.get("comments") or 0)})
    for h in load(v2_old).get("hits", [])[:8]:
        src = h.get("_source") or {}
        urls.append(f"https://www.v2ex.com/api/replies/show.json?topic_id={h.get('_id')}")
        meta.append({"kind": "v2_old", "title": src.get("title") or "", "url": f"https://www.v2ex.com/t/{h.get('_id')}",
                     "created": (src.get("created") or "")[:10], "heat": int(src.get("replies") or 0)})
    for a in load(as_search).get("results", [])[:3]:
        if a.get("trackId"):
            urls.append(f"https://itunes.apple.com/cn/rss/customerreviews/page=1/id={a['trackId']}/sortby=mostrecent/json")
            meta.append({"kind": "app", "title": a.get("trackName") or "",
                         "url": f"https://apps.apple.com/cn/app/id{a['trackId']}",
                         "heat": int(a.get("userRatingCount") or 0), "rating": a.get("averageUserRating")})
    return {"urls": urls, "meta": json.dumps(meta, ensure_ascii=False)}


def merge_posts(hn_body: str, gh_body: str, v2_body: str, meta: str = "[]", bodies: list = None,
                since_ts: int = 0, alive_ts: int = 0) -> dict:
    """各来源的原始响应 → 统一格式、去重、截断。输出 posts（JSON 字符串）和给模型看的编号列表。

    前三个参数是"新帖"（时间窗内发的）；meta + bodies 是第二轮抓取的结果（老热帖的回复、App Store 评论），
    老热帖只有近期还有回复才保留，每个来源最多 6 条，给模型看的是标题 + 最新 3 条回复。"""
    import html
    import json
    import re
    import time
    from datetime import datetime

    def clean(s, n=500):
        s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
        return re.sub(r"\s+", " ", s).strip()[:n]

    def load(body):
        try:
            return json.loads(body or "{}")
        except (TypeError, ValueError):
            return {}

    def ts(s):
        try:
            return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
        except ValueError:
            return 0

    day = lambda t: time.strftime("%Y-%m-%d", time.localtime(t)) if t else ""
    posts = []
    for h in load(hn_body).get("hits", [])[:30]:
        text = clean(h.get("comment_text") or h.get("story_text") or "")
        title = clean(h.get("title") or h.get("story_title") or "", 200)
        posts.append({"source": "HN", "title": title, "text": text or title,
                      "url": f"https://news.ycombinator.com/item?id={h.get('objectID')}",
                      "heat": int(h.get("points") or 0), "date": day(h.get("created_at_i"))})
    for it in load(gh_body).get("items", [])[:30]:
        reactions = (it.get("reactions") or {}).get("total_count", 0)
        posts.append({"source": "GitHub", "title": clean(it.get("title"), 200),
                      "text": clean(it.get("body")) or clean(it.get("title"), 200),
                      "url": it.get("html_url", ""), "heat": int(reactions) + int(it.get("comments") or 0),
                      "date": (it.get("created_at") or "")[:10]})
    for h in load(v2_body).get("hits", [])[:30]:
        src = h.get("_source") or {}
        posts.append({"source": "V2EX", "title": clean(src.get("title"), 200),
                      "text": clean(src.get("content")) or clean(src.get("title"), 200),
                      "url": f"https://www.v2ex.com/t/{h.get('_id')}", "heat": int(src.get("replies") or 0),
                      "date": (src.get("created") or "")[:10]})

    # 第二轮：老热帖（近期仍有回复才要）、App Store 评论（只要 3 星及以下 = 差评里才有痛点）
    old_n = {"GitHub": 0, "V2EX": 0}
    apps = []
    for m, body in zip(json.loads(meta or "[]"), bodies or []):
        data = load(body)
        if m["kind"] in ("gh_old", "v2_old"):
            src = "GitHub" if m["kind"] == "gh_old" else "V2EX"
            replies = data if isinstance(data, list) else []
            if src == "GitHub":
                recent = [(ts(r.get("created_at")), r.get("body")) for r in replies   # 机器人的自动回复不算有人在聊
                          if isinstance(r, dict) and (r.get("user") or {}).get("type") != "Bot"]
            else:
                recent = [(int(r.get("created") or 0), r.get("content")) for r in replies if isinstance(r, dict)]
            recent = sorted(x for x in recent if x[0] >= alive_ts)[-3:]
            if not recent or old_n[src] >= 6:
                continue
            old_n[src] += 1
            text = " / ".join(clean(b, 160) for _, b in recent)
            posts.append({"source": src, "title": clean(m["title"], 200), "text": text, "url": m["url"],
                          "heat": m["heat"], "date": m["created"], "old": True, "last_reply": day(recent[-1][0])})
        elif m["kind"] == "app":
            heat = f"{m['heat'] / 10000:.0f} 万" if m["heat"] >= 10000 else str(m["heat"])
            apps.append(f"{m['title'].split('-')[0].strip()}（{heat}人评分）")   # 去掉"-3秒钟快速记账手机助手"这类副标题
            entries = (data.get("feed") or {}).get("entry") if isinstance(data, dict) else None
            entries = [entries] if isinstance(entries, dict) else entries or []
            # 差评条条都是抱怨（实测 26 条全判为需求，占了一半以上），所以每个 App 只留 3 条写得最具体的（按字数）
            cands = []
            for e in entries:
                star = int(((e.get("im:rating") or {}).get("label")) or 0)
                when = ts((e.get("updated") or {}).get("label"))
                text = clean((e.get("content") or {}).get("label"))
                if 1 <= star <= 3 and when >= since_ts and len(text) >= 15:
                    cands.append((len(text), star, when, text, e))
            for _, star, when, text, e in sorted(cands, key=lambda c: -c[0])[:3]:
                posts.append({"source": "AppStore", "title": clean(f"{m['title']}（{star} 星）{(e.get('title') or {}).get('label', '')}", 200),
                              "text": text, "url": m["url"], "heat": 0,
                              "date": day(when), "key": f"{m['url']}#{(e.get('id') or {}).get('label', when)}"})

    seen, out = set(), []
    for p in posts:
        key = p.get("key") or p["url"] or p["title"]   # App Store 评论没有单独链接，按评论 ID 去重
        if not p["text"] or key in seen:
            continue
        seen.add(key)
        p["id"] = f"P{len(out) + 1}"
        out.append(p)

    def head(p):
        when = f"{p['date'][:4]} 年老帖，最近回复 {p['last_reply']}" if p.get("old") else p.get("date", "")
        return f"[{p['id']}]（{p['source']}，{when}）{p['title']}\n{p['text']}"

    # 按来源分几份列表，下游几个模型节点并行判断（实测一次判 90 条要 114 秒）
    names = ("HN", "GitHub", "V2EX") + (("AppStore",) if bodies is not None else ())
    listing = {s: "\n\n".join(head(p) for p in out if p["source"] == s) or "（没有帖子）" for s in names + ("AppStore",)}
    counts = {s: sum(p["source"] == s for p in out) for s in names}
    return {"posts": json.dumps(out, ensure_ascii=False), "listing_hn": listing["HN"],
            "listing_gh": listing["GitHub"], "listing_v2": listing["V2EX"], "listing_as": listing["AppStore"],
            "count": len(out), "source_counts": json.dumps(counts, ensure_ascii=False),
            "old_count": sum(old_n.values()), "apps": "、".join(apps)}


def verify_needs(llm_hn: str, llm_gh: str, llm_v2: str, posts: str, llm_as: str = "") -> dict:
    """防编造：模型摘的"原话"必须真的出现在那条帖子里（忽略空白和大小写），否则丢弃这条判断。
    llm_* 是各来源各自的判断结果（并行的几个模型节点）。"""
    import json
    import re
    norm = lambda s: re.sub(r"\s+", "", s or "").lower()
    by_id = {p["id"]: p for p in json.loads(posts)}
    items = []
    for text in (llm_hn, llm_gh, llm_v2, llm_as):
        m = re.search(r"\[.*\]", text or "", re.S)
        try:
            items += json.loads(m.group()) if m else []
        except ValueError:
            pass
    needs, dropped = [], []
    for it in items:
        p = by_id.get(str(it.get("id")))
        if not p or not it.get("is_need"):
            continue
        quote = str(it.get("quote") or "")
        if len(norm(quote)) < 4 or norm(quote) not in norm(p["title"] + " " + p["text"]):
            dropped.append(str(it.get("id")))
            continue
        needs.append({"id": p["id"], "source": p["source"], "url": p["url"], "heat": p["heat"],
                      "quote": quote, "pain_type": str(it.get("pain_type") or ""),
                      "pay_signal": bool(it.get("pay_signal")), "date": p.get("date", ""),
                      "old": bool(p.get("old"))})
    listing = "\n".join(f"[{n['id']}]（{n['source']}，{'有' if n['pay_signal'] else '无'}付费信号）{n['quote']}"
                        for n in needs)
    return {"needs": json.dumps(needs, ensure_ascii=False), "listing": listing, "need_count": len(needs),
            "dropped": len(dropped), "classified": len(items)}


def finalize_points(llm_text: str, needs: str, topic: str) -> dict:
    """痛点聚类结果的校验与打分。

    - 证据编号必须是上一步核对过的需求，无效编号剔除；没有有效证据的痛点整条丢弃；
    - 频次 = 有效证据条数（程序数，不信模型报的数）；付费信号 = 证据里有付费信号的条数；
    - 总分 = 痛度 40% + 频次 30% + 付费意愿 30%（都折算到 0～5），可解释、可复算。
    """
    import json
    import re
    by_id = {n["id"]: n for n in json.loads(needs)}
    m = re.search(r"\[.*\]", llm_text or "", re.S)
    try:
        raw = json.loads(m.group()) if m else []
    except ValueError:
        raw = []
    points = []
    for r in raw:
        ev = []
        for i in r.get("evidence", []):
            if str(i) in by_id and str(i) not in ev:
                ev.append(str(i))
        if not ev:
            continue
        pain = min(max(float(r.get("pain") or 1), 1), 5)
        # 同一个 App 的差评最多算 2 次：某个 App 自己的毛病（如广告多）不该靠条数刷成头号痛点
        counted, per_app = [], {}
        for i in ev:
            if by_id[i]["source"] == "AppStore":
                per_app[by_id[i]["url"]] = per_app.get(by_id[i]["url"], 0) + 1
                if per_app[by_id[i]["url"]] > 2:
                    continue
            counted.append(i)
        pay = sum(by_id[i]["pay_signal"] for i in counted)
        freq_score = min(len(counted) / 5, 1) * 5
        sources = {}
        for i in ev:
            sources[by_id[i]["source"]] = sources.get(by_id[i]["source"], 0) + 1
        pay_score = min(pay / 2, 1) * 5
        points.append({"name": str(r.get("name") or "")[:40], "who": str(r.get("who") or "")[:60],
                       "description": str(r.get("description") or "")[:200],
                       "workaround": str(r.get("workaround") or "")[:120],
                       "pain": pain, "frequency": len(counted), "pay_count": pay, "sources": sources,
                       "score": round(0.4 * pain + 0.3 * freq_score + 0.3 * pay_score, 2),
                       "evidence": [{"id": i, "url": by_id[i]["url"], "source": by_id[i]["source"],
                                     "quote": by_id[i]["quote"], "date": by_id[i].get("date", ""),
                                     "old": by_id[i].get("old", False)} for i in ev]})
    points.sort(key=lambda p: -p["score"])
    top = points[0] if points else None
    competitor_topic = f"{topic}：{top['name']}（{top['description']}）" if top else topic
    return {"points": json.dumps(points, ensure_ascii=False), "point_count": len(points),
            "competitor_topic": competitor_topic[:150]}


def render_report(points: str, comp_json: list, comp_text: str, topic: str, source_counts: str, post_count: int,
                  need_count: int, dropped: int, months: int = 0, old_count: int = 0, apps: str = "") -> dict:
    """报告由程序渲染：链接、频次、分数都来自前面核对过的数据，模型不碰链接。

    MCP 工具返回的 {"markdown", "report"} 在 Dify 里落在工具节点的 json 输出（列表），text 是空的（实测）；
    工具失败时默认值落在 text。两个都接，哪个有内容用哪个。

    篇幅控制（一眼能看完）：排行最多 6 个痛点；只展开前 3 名，每个最多 3 条证据（优先不同来源）；
    竞品只取总结、竞品名、最多 3 个没解决好的地方和 3 条带链接的事实。"""
    import json
    pts = json.loads(points or "[]")[:6]
    counts = json.loads(source_counts or "{}")
    comp = next((x for x in (comp_json or []) if isinstance(x, dict) and (x.get("markdown") or x.get("report"))), None)
    if comp is None:
        try:
            comp = json.loads(comp_text)
        except (ValueError, TypeError):
            comp = {"markdown": comp_text or ""}
    comp = comp if isinstance(comp, dict) else {}
    rep = comp.get("report") if isinstance(comp.get("report"), dict) else {}

    src = lambda s: "App Store" if s == "AppStore" else s
    line = " · ".join(f"{src(k)} {v}" for k, v in counts.items())
    window = f"近 {months} 个月的讨论（另含 {old_count} 条更早但近期仍有人回复的老帖）· " if months else ""
    out = [f"# 📡 需求雷达：{topic}\n",
           f"> {window}抓取 {post_count} 条（{line}）→ 需求 {need_count} 条"
           f"{f'（{dropped} 条原话对不上已丢弃）' if dropped else ''} → 痛点 {len(pts)} 个\n",
           "## 痛点排行\n", "| # | 痛点 | 谁在痛 | 总分 | 提及 | 来源 |", "|:-:|---|---|:-:|:-:|---|"]
    for i, p in enumerate(pts, 1):
        where = " · ".join(f"{src(k)} {v}" for k, v in (p.get("sources") or {}).items())
        out.append(f"| {i} | **{p['name']}** | {p['who']} | {p['score']} | {p['frequency']} | {where} |")

    for i, p in enumerate(pts[:3], 1):
        out += [f"\n### {i}. {p['name']}　`{p['score']} 分`\n", p["description"]]
        if p["workaround"]:
            out.append(f"\n- **现在的土办法**：{p['workaround']}")
        ev, seen = [], set()
        for e in p["evidence"] + p["evidence"]:   # 第一遍每个来源取一条，第二遍补满
            if len(ev) < 3 and e not in ev and (e["source"] not in seen or len(seen) >= len({x["source"] for x in p["evidence"]})):
                ev.append(e)
                seen.add(e["source"])
        out.append("")
        out += [f"> 「{e['quote']}」（[{src(e['source'])} {e['id']}]({e['url']})"
                f"{'，' + e['date'] if e.get('date') else ''}{'，老帖·近期仍有讨论' if e.get('old') else ''}）  "
                for e in ev]
        if len(p["evidence"]) > len(ev):
            out.append(f"> …另有 {len(p['evidence']) - len(ev)} 条")

    top_name = "：" + pts[0]["name"] if pts else ""
    out.append(f"\n## 🔍 头号痛点的竞品{top_name}\n")
    if rep.get("summary"):
        out.append(f"> {rep['summary']}\n")
        if rep.get("competitors"):
            out.append(f"- **主要竞品**：{'、'.join(rep['competitors'][:6])}")
        cut = lambda s, n: s if len(s) <= n else s[:n] + "…"
        if rep.get("gaps"):   # 实测每条空白点能写到 100 多字，截短
            out.append("- **还没解决好的**：" + "；".join(cut(g.split("：")[0] if "：" in g[:20] else g, 40)
                                                     for g in rep["gaps"][:3]))
        facts = [f for f in rep.get("facts", []) if f.get("category") in ("定价", "用户评价与抱怨")][:3]
        out += [f"- {f['competitor']}：{cut(f['content'], 60)}（[来源]({f['source_url']})）" for f in facts]
    else:
        out.append(comp.get("markdown") or "（竞品调研没有返回结果）")
    if apps:
        out.append(f"- **App Store 同类热门**：{apps}")
    out += ["\n---", "<sub>总分 = 痛度 40% + 提及 30% + 付费信号 30%；提及、付费信号由程序按证据条数统计，"
                     "同一个 App 的差评最多算 2 条。每条证据都附原帖链接。</sub>"]
    return {"report": "\n".join(out)}
