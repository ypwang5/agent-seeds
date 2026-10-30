"""需求雷达评测（在 job-agent 目录下运行，复用它的模型适配和调研 Agent）：

    cd ../job-agent; uv run python ../demand-radar/eval/run_eval.py [classify|compare|all]

1. classify：4 个领域抓真实帖子（与工作流同样的接口和清洗代码），用工作流同一份提示词让快模型判断"是不是需求"，
   再用强模型独立判断一遍作参照。输出 eval/labels.csv（含 human 空列，供人工标注）和一致率；
   两个模型不一致的帖子单独列出，供人工裁决。
2. compare：竞品调研的两种做法——
   A. 纯工作流：一个模型节点直接问"有哪些竞品，附来源链接"（没有搜索工具，最常见的做法）；
   B. 内核调研 Agent（经 MCP 调用的那个）：搜索 + 来源核对。
   指标：给出的链接数、能打开的比例（HTTP 状态 < 400）、耗时、成本。
"""
import csv
import re
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from langchain_core.callbacks import get_usage_metadata_callback

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
import code_nodes as C  # noqa: E402
import prompts as P  # noqa: E402

from job_agent.core.llm import get_model  # noqa: E402
from job_agent.radar.competitor import research_competitors  # noqa: E402

TOPICS = {"记账": ("expense tracking app", "记账 软件"), "笔记软件": ("note taking app", "笔记 软件"),
          "简历工具": ("resume builder", "简历 工具"), "家庭网络": ("home network router", "家里 路由器")}
FLASH, PRO = "deepseek:deepseek-flash", "deepseek:deepseek-v4-pro"
PRICE = {FLASH: (2, 8), PRO: (9, 27)}   # 元 / 百万 tokens，同 job-agent 的多模型对比
OUT = HERE / "out"


def fetch(en: str, zh: str) -> dict:
    """和工作流里三个 HTTP 节点一样的请求参数。"""
    get = lambda url, params, headers=None: httpx.get(url, params=params, headers=headers, timeout=30).text
    return C.merge_posts(
        get("https://hn.algolia.com/api/v1/search", {"query": en, "tags": "(story,comment)", "hitsPerPage": 30}),
        get("https://api.github.com/search/issues", {"q": f"{en} in:title is:issue", "per_page": 30},
            {"User-Agent": "demand-radar"}),
        get("https://www.sov2ex.com/api/search", {"q": zh, "size": 30, "sort": "sumup"}))


def classify(spec: str, topic: str, listing: str) -> dict:
    prompt = P.CLASSIFY_SYSTEM.replace("{{#start.topic#}}", topic)
    text = get_model(spec).invoke([{"role": "system", "content": prompt}, {"role": "user", "content": listing}]).content
    m = re.search(r"\[.*\]", text, re.S)
    return {str(x.get("id")): x for x in (json.loads(m.group()) if m else [])}


def run_classify():
    rows, agree, total, disputes = [], 0, 0, []
    for topic, (en, zh) in TOPICS.items():
        merged = fetch(en, zh)
        posts = json.loads(merged["posts"])
        listings = [merged["listing_hn"], merged["listing_gh"], merged["listing_v2"]]
        with ThreadPoolExecutor(6) as pool:   # 和工作流一样按来源分三批
            fl = pool.map(lambda l: classify(FLASH, topic, l), listings)
            pr = pool.map(lambda l: classify(PRO, topic, l), listings)
            fast = {k: v for d in fl for k, v in d.items()}
            ref = {k: v for d in pr for k, v in d.items()}
        for p in posts:
            f, r = bool(fast.get(p["id"], {}).get("is_need")), bool(ref.get(p["id"], {}).get("is_need"))
            total += 1
            agree += f == r
            row = {"topic": topic, "id": p["id"], "source": p["source"], "title": p["title"], "text": p["text"],
                   "url": p["url"], "flash": int(f), "pro": int(r), "human": ""}
            rows.append(row)
            if f != r:
                disputes.append(row)
        print(f"{topic}：{len(posts)} 条，快模型判需求 {sum(bool(v.get('is_need')) for v in fast.values())}，"
              f"强模型 {sum(bool(v.get('is_need')) for v in ref.values())}", flush=True)
    OUT.mkdir(exist_ok=True)
    with open(OUT / "labels.csv", "w", encoding="utf-8-sig", newline="") as f:   # utf-8-sig：Excel 打开不乱码
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    (OUT / "disputes.json").write_text(json.dumps(disputes, ensure_ascii=False, indent=1), encoding="utf-8")
    by_src = {s: [r for r in rows if r["source"] == s] for s in ("HN", "GitHub", "V2EX")}
    print(f"\n共 {total} 条；快模型与强模型一致 {agree} 条（{agree / total:.0%}）；不一致 {len(disputes)} 条见 out/disputes.json")
    for s, rs in by_src.items():
        print(f"  {s}：{len(rs)} 条，强模型判为需求 {sum(r['pro'] for r in rs)} 条")




NAIVE_PROMPT = """调研这个需求方向的现有竞品：{topic}。
列出主要竞品，每个竞品写：产品功能、定价、用户评价与抱怨，每条信息都附上来源链接（URL）。"""


def status(url: str) -> int:
    """HTTP 状态码；连不上记 0。403 多半是网站拦爬虫，不代表链接是假的，所以只把 404/410 当"地址不存在"。"""
    try:
        return httpx.get(url, timeout=15, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}).status_code
    except httpx.HTTPError:
        return 0


def link_stats(urls: list[str]) -> dict:
    with ThreadPoolExecutor(8) as pool:
        codes = dict(zip(urls, pool.map(status, urls)))
    home = [u for u in urls if httpx.URL(u).path in ("", "/")]
    deep = [u for u in urls if u not in home]
    return {"n": len(urls), "home": len(home), "deep": len(deep),
            "deep_404": sum(codes[u] in (404, 410) for u in deep), "codes": codes}


def run_compare():
    lines = ["| 领域 | 做法 | 链接 | 其中首页 | 具体页面 | 具体页面 404 | 耗时 | 成本（分） |",
             "|---|---|---|---|---|---|---|---|"]
    detail = {}
    for topic in TOPICS:
        t = time.time()
        with get_usage_metadata_callback() as cb:
            text = get_model(FLASH).invoke(NAIVE_PROMPT.format(topic=topic)).content
        naive_cost = sum(u["input_tokens"] * PRICE[FLASH][0] + u["output_tokens"] * PRICE[FLASH][1]
                         for u in cb.usage_metadata.values()) / 1e6
        a = link_stats(sorted(set(re.findall(r"https?://[^\s)\]）>\"'，。]+", text)))) | {
            "sec": time.time() - t, "cost": naive_cost}

        t = time.time()
        with get_usage_metadata_callback() as cb:
            rep = research_competitors(topic, use_cache=False)
        kernel_cost = sum(u["input_tokens"] * PRICE[PRO][0] + u["output_tokens"] * PRICE[PRO][1]
                          for u in cb.usage_metadata.values()) / 1e6
        b = link_stats(sorted({f.source_url for f in rep.facts})) | {"sec": time.time() - t, "cost": kernel_cost}
        detail[topic] = {"naive": a, "kernel": b}
        for name, s in [("A 纯模型节点", a), ("B 内核调研 Agent", b)]:
            lines.append(f"| {topic} | {name} | {s['n']} | {s['home']} | {s['deep']} | {s['deep_404']} "
                         f"| {s['sec']:.0f}s | {s['cost'] * 100:.2f} |")
        print(lines[-2], lines[-1], sep="\n", flush=True)
    OUT.mkdir(exist_ok=True)
    (OUT / "compare.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUT / "compare_detail.json").write_text(json.dumps(detail, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what in ("classify", "all"):
        run_classify()
    if what in ("compare", "all"):
        run_compare()
