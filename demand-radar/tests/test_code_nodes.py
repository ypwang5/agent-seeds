"""代码节点单测：数据源用真实接口的响应（tests/fixtures），模型输出用手写的假数据。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))
import code_nodes as C  # noqa: E402

FIX = ROOT / "tests" / "fixtures"


def merged():
    return C.merge_posts(*(FIX.joinpath(f"{s}.json").read_text(encoding="utf-8") for s in ("hn", "gh", "v2")))


def test_parse_queries_falls_back_to_topic():
    assert C.parse_queries('好的：{"en": "expense app", "zh": "记账"}', "记账") == {"en": "expense app", "zh": "记账"}
    assert C.parse_queries("不是 JSON", "记账") == {"en": "记账", "zh": "记账"}


def test_merge_real_responses():
    out = merged()
    posts = json.loads(out["posts"])
    counts = json.loads(out["source_counts"])
    assert out["count"] == len(posts) > 0 and all(counts[s] > 0 for s in ("HN", "GitHub", "V2EX"))
    assert [p["id"] for p in posts] == [f"P{i}" for i in range(1, len(posts) + 1)]
    assert all(p["url"].startswith("https://") and "<" not in p["text"] for p in posts)   # 去掉了 HTML
    assert len({p["url"] for p in posts}) == len(posts)                                      # 去重


def test_merge_survives_failed_source():
    out = C.merge_posts("{}", "not json", FIX.joinpath("v2.json").read_text(encoding="utf-8"))
    assert json.loads(out["source_counts"]) == {"HN": 0, "GitHub": 0, "V2EX": out["count"]}


def test_verify_drops_fabricated_quotes():
    posts = json.loads(merged()["posts"])
    p1, p2 = posts[0], posts[1]
    real = p1["text"][:20]
    llm = json.dumps([
        {"id": p1["id"], "is_need": True, "quote": real, "pain_type": "体验差", "pay_signal": True},
        {"id": p2["id"], "is_need": True, "quote": "这句话帖子里根本没有", "pain_type": "其他", "pay_signal": False},
        {"id": "P999", "is_need": True, "quote": real, "pain_type": "其他", "pay_signal": False},   # 不存在的编号
    ], ensure_ascii=False)
    # 三个来源并行判断：一份正常、一份空、一份坏 JSON，坏的不能拖垮整体
    out = C.verify_needs("```json\n" + llm + "\n```", "", "不是 JSON", json.dumps(posts, ensure_ascii=False))
    needs = json.loads(out["needs"])
    assert [n["id"] for n in needs] == [p1["id"]] and out["dropped"] == 1
    assert needs[0]["url"] == p1["url"]          # 链接来自抓取结果，不来自模型


def test_finalize_counts_and_scores_by_program():
    needs = json.dumps([
        {"id": "P1", "source": "HN", "url": "u1", "heat": 1, "quote": "q1", "pain_type": "", "pay_signal": True},
        {"id": "P2", "source": "V2EX", "url": "u2", "heat": 1, "quote": "q2", "pain_type": "", "pay_signal": False},
    ])
    llm = json.dumps([
        {"name": "同步麻烦", "who": "个人", "description": "d", "workaround": "", "pain": 4,
         "evidence": ["P1", "P2", "P2", "P77"]},                       # 重复编号、无效编号要剔除
        {"name": "没有证据的痛点", "pain": 5, "evidence": ["P88"]},      # 整条丢弃
    ], ensure_ascii=False)
    out = C.finalize_points(llm, needs, "记账")
    pts = json.loads(out["points"])
    assert len(pts) == 1 and pts[0]["frequency"] == 2 and pts[0]["pay_count"] == 1
    assert pts[0]["score"] == round(0.4 * 4 + 0.3 * 2 + 0.3 * 2.5, 2)
    assert out["competitor_topic"].startswith("记账：同步麻烦")


def test_render_report_uses_verified_links_only():
    pts = json.dumps([{"name": "同步麻烦", "who": "个人", "description": "d", "workaround": "用 Excel", "pain": 4,
                       "frequency": 1, "pay_count": 0, "score": 2.5,
                       "evidence": [{"id": "P1", "url": "https://x/1", "source": "HN", "quote": "q1"}]}],
                     ensure_ascii=False)
    comp = [{"markdown": "## 竞品调研：记账", "report": {}}]   # Dify 里 MCP 工具的结构化返回落在 json 输出
    md = C.render_report(pts, comp, "", "记账", '{"HN": 3}', 3, 1, 0)["report"]
    assert "[HN P1](https://x/1)" in md and "## 竞品调研：记账" in md and "用 Excel" in md
    assert "竞品调研服务暂不可用" in C.render_report(pts, [], "（竞品调研服务暂不可用）", "记账", "{}", 0, 0, 0)["report"]
