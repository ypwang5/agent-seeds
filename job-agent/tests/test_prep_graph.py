"""主图的离线测试：所有模型调用换成假的，只测流程编排、interrupt 暂停与恢复。"""
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from job_agent.job import prep_graph as pg
from job_agent.job.gap import GapReport, Match
from job_agent.job.research import CompanyReport
from job_agent.job.rewrite import RewriteItem
from job_agent.job.schemas import JobPosting

LINES = ["参与物流调度系统研发，日均处理 120 万笔"]


def fake_all(monkeypatch, new_text: str):
    monkeypatch.setattr(pg, "parse_jd", lambda text: JobPosting(title="AI 应用开发", company="某公司",
                                                                hard_requirements=["熟悉 Java"]))
    monkeypatch.setattr(pg, "analyze_gap", lambda jd, lines: GapReport(
        ["熟悉 Java"], [Match(req_id=1, status="部分", reason="")], []))
    monkeypatch.setattr(pg, "research_company", lambda company, title: CompanyReport(company=company, summary="s"))
    monkeypatch.setattr(pg, "rewrite_resume", lambda gap, title, lines: [RewriteItem(
        lines=[1], original=LINES[0], new_text=new_text, change="")])
    # 模型复核换成"全部通过"，只留程序检查起作用
    from job_agent.job import rewrite
    monkeypatch.setattr(rewrite, "model_review", lambda pairs, role="strong": [
        rewrite.Verdict(index=i, verdict="通过", reason="") for i in range(len(pairs))])
    monkeypatch.setattr(pg, "generate_questions", lambda *a, **k: ([], []))


def run(graph, inp, thread="t1"):
    config = {"configurable": {"thread_id": thread}}
    graph.invoke(inp, config)
    return graph.get_state(config), config


def test_clean_rewrite_skips_review(monkeypatch):
    fake_all(monkeypatch, "参与物流调度系统研发（日均 120 万笔运单）")
    state, _ = run(pg.build_graph(InMemorySaver()), {"jd_text": "jd", "lines": LINES, "problems": []})
    assert not state.next
    assert state.values["rewrites"][0]["decision"] == "自动通过"
    assert "面试准备" in state.values["report_md"]


def test_fabricated_rewrite_pauses_then_resume(monkeypatch):
    fake_all(monkeypatch, "主导物流调度系统研发，日均处理 500 万笔")
    graph = pg.build_graph(InMemorySaver())
    state, config = run(graph, {"jd_text": "jd", "lines": LINES, "problems": []})
    assert state.next == ("review",)  # 停在人工确认
    payload = state.tasks[0].interrupts[0].value
    assert payload["items"][0]["rule_problems"] == ["数字 500 在原文中没有", "疑似拔高：原文没有'主导'"]

    graph.invoke(Command(resume={"0": {"action": "reject"}}), config)
    state = graph.get_state(config)
    assert not state.next
    r = state.values["rewrites"][0]
    assert r["decision"] == "已拒绝，保留原文" and r["final_text"] == LINES[0]
