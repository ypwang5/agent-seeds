"""反馈计数、改写套回简历、Word 导出的离线测试。"""
import io

from docx import Document
from fastapi.testclient import TestClient

from job_agent import api
from job_agent.feedback import FeedbackStore
from job_agent.job.export import apply_rewrites, to_docx

RESUME = "张三\n参与物流调度系统研发，处理跨省及加\n急运单数据\n熟悉 Java"


def test_feedback_store_counts(tmp_path):
    fb = FeedbackStore(tmp_path / "f.sqlite")
    s = "a" * 32
    fb.vote(s, "rewrite", 0, "up")
    fb.vote(s, "rewrite", 0, "down")      # 同一条改主意：只留最后一次
    fb.vote(s, "question", 1, "up")
    fb.vote(s, "question", 1, None)       # 取消
    fb.record_run(s, [{"decision": "自动通过"}, {"decision": "已拒绝，保留原文"}, {"decision": "人工修改"}])
    st = fb.stats()
    assert st["votes"] == {"rewrite_down": 1}
    assert st["rewrites"] == {"total": 3, "auto_pass": 1, "accepted": 0, "edited": 1, "rejected": 1}
    assert st["kept_rate"] == round(2 / 3, 3)


def test_apply_rewrites_handles_pdf_line_break_and_misses():
    rewrites = [
        # 原文跨了 PDF 断行（第 2、3 行），模型只标了第 2 行
        {"lines": [2], "original": "处理跨省及加急运单数据", "final_text": "处理跨省与加急运单数据（物流调度）"},
        {"lines": [4], "original": "熟悉 Java", "final_text": "熟悉 Java"},              # 被拒绝的：原样
        {"lines": [4], "original": "精通 Go", "final_text": "熟练使用 Go"},              # 原文里找不到
    ]
    paragraphs, missed = apply_rewrites(RESUME, rewrites)
    assert paragraphs == [("张三", False), ("参与物流调度系统研发，处理跨省与加急运单数据（物流调度）", True),
                          ("熟悉 Java", False)]
    assert [m["original"] for m in missed] == ["精通 Go"]


def test_docx_highlights_changes_and_lists_missed():
    data = to_docx([("不变", False), ("改过", True)], [{"original": "a", "final_text": "b"}])
    doc = Document(io.BytesIO(data))
    texts = [p.text for p in doc.paragraphs]
    assert "改过" in texts and "原文：a" in texts
    changed = next(p for p in doc.paragraphs if p.text == "改过")
    assert changed.runs[0].font.highlight_color is not None


def test_feedback_and_export_endpoints(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "FEEDBACK", FeedbackStore(tmp_path / "f.sqlite"))
    client = TestClient(api.app)
    assert client.post("/api/feedback", json={"session": "b" * 32, "kind": "screen", "idx": 0,
                                              "vote": "up"}).status_code == 200
    assert client.post("/api/feedback", json={"session": "../etc", "kind": "screen", "idx": 0}).status_code == 400
    assert client.get("/api/stats").json()["votes"] == {"screen_up": 1}

    r = client.post("/api/export/resume", json={"resume_text": RESUME, "rewrites": [
        {"lines": [4], "original": "熟悉 Java", "final_text": "熟悉 Java 与 Spring"}]})
    assert r.status_code == 200 and r.headers["x-missed"] == "0"
    assert "熟悉 Java 与 Spring" in [p.text for p in Document(io.BytesIO(r.content)).paragraphs]
