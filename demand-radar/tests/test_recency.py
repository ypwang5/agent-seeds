"""时效性 + App Store 源的单测：夹具是 2026-10-30 按 DSL 同样参数抓的真实响应（tests/fixtures/recency）。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))
import code_nodes as C  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "recency"
W = json.loads(FIX.joinpath("window.json").read_text(encoding="utf-8"))   # 抓夹具时的时间窗


def read(name):
    return FIX.joinpath(name).read_text(encoding="utf-8")


def merged():
    return C.merge_posts(read("hn.json"), read("gh.json"), read("v2.json"), read("plan_meta.json"),
                         json.loads(read("follow_bodies.json")), W["since_ts"], W["alive_ts"])


def test_time_window_defaults_and_bounds():
    w = C.time_window(None)
    assert w["months"] == 12 and w["alive_ts"] > w["since_ts"]          # 老帖"还活着"的门槛比新帖起点更近
    assert C.time_window("abc")["months"] == 12 and C.time_window(999)["months"] == 60
    assert C.time_window(3)["alive_ts"] == C.time_window(3)["since_ts"]  # 窗口小于 6 个月时两者重合


def test_plan_followups_aligned():
    out = C.plan_followups(read("gh_old.json"), read("v2_old.json"), read("as_search.json"), W["alive_date"])
    meta = json.loads(out["meta"])
    assert len(meta) == len(out["urls"]) > 0
    kinds = {m["kind"] for m in meta}
    assert kinds == {"gh_old", "v2_old", "app"}
    for u, m in zip(out["urls"], meta):
        if m["kind"] == "gh_old":
            assert f"since={W['alive_date']}" in u
        if m["kind"] == "app":
            assert "customerreviews" in u and m["url"].startswith("https://apps.apple.com/cn/app/id")


def test_plan_reads_appstore_from_extracted_file_when_body_empty():
    # Dify 实测：苹果搜索接口按附件返回，HTTP 节点 body 为空，文本要从文档提取器拿
    out = C.plan_followups("{}", "{}", "", W["alive_date"], [read("as_search.json")])
    assert [m["kind"] for m in json.loads(out["meta"])] == ["app"] * 3
    assert C.plan_followups("{}", "{}", "", W["alive_date"], [])["urls"] == []


def test_merge_keeps_recent_and_live_old_posts():
    out = merged()
    posts = json.loads(out["posts"])
    new = [p for p in posts if not p.get("old")]
    old = [p for p in posts if p.get("old")]
    assert all(p["date"] >= W["since_date"] for p in new if p["source"] != "AppStore")   # 新帖都在时间窗内
    assert all(p["date"] < W["since_date"] and p["last_reply"] >= W["alive_date"] for p in old)
    assert out["old_count"] == len(old) and len([p for p in old if p["source"] == "V2EX"]) <= 6
    assert [p["id"] for p in posts] == [f"P{i}" for i in range(1, len(posts) + 1)]
    counts = json.loads(out["source_counts"])
    assert set(counts) == {"HN", "GitHub", "V2EX", "AppStore"} and counts["AppStore"] > 0


def test_appstore_only_low_star_reviews_and_not_deduped_by_app_url():
    posts = [p for p in json.loads(merged()["posts"]) if p["source"] == "AppStore"]
    assert posts and all("（1 星）" in p["title"] or "（2 星）" in p["title"] or "（3 星）" in p["title"] for p in posts)
    assert len({p["url"] for p in posts}) < len(posts)          # 同一个 App 的多条评论都保留
    assert len({p["key"] for p in posts}) == len(posts)


def test_appstore_capped_per_app_and_prefers_specific_reviews():
    posts = [p for p in json.loads(merged()["posts"]) if p["source"] == "AppStore"]
    per_app = {}
    for p in posts:
        per_app[p["url"]] = per_app.get(p["url"], 0) + 1
    assert per_app and max(per_app.values()) <= 3
    assert all(len(p["text"]) >= 15 for p in posts)          # 太短的（"垃圾"）不要


def test_same_app_reviews_count_at_most_twice():
    needs = json.dumps([{"id": f"P{i}", "source": "AppStore", "url": "https://apps.apple.com/cn/app/id1", "heat": 0,
                         "quote": f"q{i}", "pain_type": "", "pay_signal": True} for i in range(1, 6)]
                       + [{"id": "P9", "source": "V2EX", "url": "u9", "heat": 0, "quote": "q9", "pain_type": "",
                           "pay_signal": False}])
    llm = json.dumps([{"name": "广告多", "pain": 5, "evidence": ["P1", "P2", "P3", "P4", "P5", "P9"]}], ensure_ascii=False)
    p = json.loads(C.finalize_points(llm, needs, "记账")["points"])[0]
    assert p["frequency"] == 3 and p["pay_count"] == 2           # 同一 App 5 条只算 2 条 + V2EX 1 条
    assert p["sources"] == {"AppStore": 5, "V2EX": 1} and len(p["evidence"]) == 6   # 证据照样全部保留


def test_report_is_compact():
    ev = lambda i, s: {"id": f"P{i}", "url": f"https://x/{i}", "source": s, "quote": f"q{i}", "date": "2026-10-01"}
    pts = json.dumps([{"name": f"痛点{k}", "who": "w", "description": "d", "workaround": "", "pain": 3, "frequency": 6,
                       "pay_count": 0, "score": 9 - k, "sources": {"AppStore": 5, "V2EX": 1},
                       "evidence": [ev(k * 10 + j, "AppStore") for j in range(5)] + [ev(k * 10 + 9, "V2EX")]}
                      for k in range(8)], ensure_ascii=False)
    comp = [{"markdown": "很长的原始竞品报告", "report": {
        "summary": "已有不少产品", "competitors": ["A", "B"], "gaps": ["g1", "g2", "g3", "g4"],
        "facts": [{"competitor": "A", "category": "定价", "content": f"c{i}", "source_url": f"https://s/{i}"} for i in range(5)]}}]
    md = C.render_report(pts, comp, "", "记账", '{"AppStore": 9}', 9, 9, 0)["report"]
    assert "痛点5" in md and "痛点6" not in md                        # 排行最多 6 个
    assert md.count("### ") == 3                                      # 只展开前 3 名
    assert "App Store 5 · V2EX 1" in md                               # 来源列
    assert "[V2EX P9](https://x/9)" in md and "另有 3 条" in md         # 每个最多 3 条证据，优先不同来源
    assert "g3" in md and "g4" not in md and "c2" in md and "c3" not in md and "很长的原始竞品报告" not in md


def test_dead_old_posts_and_bot_replies_dropped():
    meta = json.dumps([
        {"kind": "v2_old", "title": "老帖A", "url": "https://www.v2ex.com/t/1", "created": "2018-01-01", "heat": 50},
        {"kind": "gh_old", "title": "老issue", "url": "https://github.com/a/b/issues/1", "created": "2020-01-01", "heat": 9},
        {"kind": "v2_old", "title": "老帖B", "url": "https://www.v2ex.com/t/2", "created": "2019-01-01", "heat": 30},
    ])
    alive = 1_700_000_000
    bodies = [
        json.dumps([{"created": alive - 10, "content": "很久以前的回复"}]),                       # 最后回复早于门槛 → 丢
        json.dumps([{"created_at": "2026-06-01T00:00:00Z", "body": "closed", "user": {"type": "Bot"}}]),  # 只有机器人 → 丢
        json.dumps([{"created": alive + i, "content": f"新回复{i}"} for i in range(5)]),             # 留，只取最新 3 条
    ]
    out = C.merge_posts("{}", "{}", "{}", meta, bodies, 0, alive)
    posts = json.loads(out["posts"])
    assert [p["title"] for p in posts] == ["老帖B"] and out["old_count"] == 1
    assert posts[0]["text"] == "新回复2 / 新回复3 / 新回复4"
    assert "2019 年老帖" in out["listing_v2"]


def test_report_shows_window_dates_and_old_marker():
    pts = json.dumps([{"name": "广告多", "who": "记账用户", "description": "d", "workaround": "", "pain": 4,
                       "frequency": 2, "pay_count": 0, "score": 2.5, "evidence": [
                           {"id": "P1", "url": "https://x/1", "source": "V2EX", "quote": "q1", "date": "2019-03-01", "old": True},
                           {"id": "P2", "url": "https://x/2", "source": "AppStore", "quote": "q2", "date": "2026-10-28"}]}],
                     ensure_ascii=False)
    md = C.render_report(pts, [], "", "记账", '{"V2EX": 1}', 2, 2, 0, months=12, old_count=1, apps="鲨鱼记账（233 万条评分）")["report"]
    assert "近 12 个月" in md and "1 条更早但近期仍有人回复" in md
    assert "[V2EX P1](https://x/1)，2019-03-01，老帖·近期仍有讨论）" in md and "2026-10-28）" in md
    assert "鲨鱼记账（233 万条评分）" in md
