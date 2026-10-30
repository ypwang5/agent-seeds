"""网页接口的离线测试：模型调用全部换成假的，测 SSE 事件、暂停/恢复、隐私还原、限额。"""
import json
import sqlite3
import time
from typing import TypedDict

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import START, StateGraph

from job_agent import api
from job_agent.core.pii import Masker
from job_agent.job import screen_graph as sg
from job_agent.job.gap import GapReport, Match
from job_agent.job.schemas import JobPosting
from test_prep_graph import fake_all

RESUME = "姓名：张小明\n电话 13812345678\n参与物流调度系统研发，日均处理 120 万笔"


def events(resp) -> list[tuple[str, dict]]:
    """把 SSE 响应体解析成 [(事件名, 数据)]。"""
    out = []
    for block in resp.text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        out.append((fields.get("event", "message"), json.loads(fields["data"])))
    return out


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(api, "_usage", api.defaultdict(int))
    return TestClient(api.app)


def test_screen_streams_progress_then_result(client, monkeypatch):
    monkeypatch.setattr(sg, "parse_jd", lambda text: JobPosting(title=text, company="某公司", hard_requirements=["a"]))
    monkeypatch.setattr(sg, "analyze_gap", lambda jd, lines, role: GapReport(
        ["a"], [Match(req_id=1, status="命中", reason="张小明做过")], []))
    monkeypatch.setattr(sg, "jd_risks", lambda text, company: ([], []))
    monkeypatch.setattr(sg, "company_risks", lambda company: ([], []))
    seen_lines = []
    real = sg.screen_one
    monkeypatch.setattr(sg, "screen_one", lambda job: seen_lines.append(job["lines"]) or real(job))
    api.SCREEN = sg.build_graph()

    resp = client.post("/api/screen", json={"resume_text": RESUME, "jds": [{"id": "1", "text": "岗位A"}]})
    evs = events(resp)
    assert [e for e, _ in evs[:-1]] == ["progress"] * 4 and evs[-1][0] == "result"
    assert evs[-1][1]["ranked"][0]["advice"] == "直接投"
    assert "张小明" not in json.dumps(seen_lines, ensure_ascii=False)                 # 进图的是脱敏后的
    assert evs[-1][1]["ranked"][0]["matches"][0]["reason"] == "张小明做过"           # 返回给用户时还原


def test_prep_pause_and_resume(client, monkeypatch):
    fake_all(monkeypatch, "主导物流调度系统研发，日均处理 500 万笔")
    evs = events(client.post("/api/prep", json={"resume_text": RESUME, "jd_text": "jd"}))
    names = [e for e, _ in evs]
    assert names[0] == "thread" and names[-1] == "review"
    thread = evs[0][1]["thread_id"]
    assert {"name": "gap", "score": 0.5} in [d for e, d in evs if e == "node"]

    bad = client.post(f"/api/prep/{thread}/resume", json={"resume_text": "另一份简历", "answers": {}})
    assert bad.status_code == 400

    evs = events(client.post(f"/api/prep/{thread}/resume", json={
        "resume_text": RESUME, "answers": {"0": {"action": "edit", "text": "张小明参与物流调度系统研发"}}}))
    assert evs[-1][0] == "report"
    assert "张小明参与物流调度系统研发" in evs[-1][1]["markdown"]
    stored = api.PREP.get_state({"configurable": {"thread_id": thread}}).values["rewrites"][0]["final_text"]
    assert "张小明" not in stored                                                   # 存档里仍是占位符

    assert client.post(f"/api/prep/{thread}/resume", json={"resume_text": RESUME, "answers": {}}).status_code == 404


def test_free_quota(client, monkeypatch):
    monkeypatch.setattr(api, "FREE_PER_DAY", 2)
    monkeypatch.setattr(api, "PREP_COST", 3)
    assert client.post("/api/prep", json={"resume_text": "x", "jd_text": "y"}).status_code == 429
    # 带自己的 Key 不受限额（这里不真正跑，只看是否放行到参数校验）
    r = client.post("/api/screen", json={"resume_text": "x", "jds": []}, headers={"X-DeepSeek-Key": "sk-test"})
    assert r.status_code == 400


def test_masker_reuses_known_names():
    m = Masker()
    m.mask("姓名：张小明")
    assert m.mask("张小明负责") == "[NAME_1]负责"


def test_real_ip_only_trusted_from_tunnel(client):
    from starlette.requests import Request as R
    req = lambda host, hdr: R({"type": "http", "client": (host, 1), "headers": [(b"cf-connecting-ip", hdr.encode())]})
    assert api.client_ip(req("127.0.0.1", "1.2.3.4")) == "1.2.3.4"     # 经 Tunnel 转发：信头
    assert api.client_ip(req("5.6.7.8", "1.2.3.4")) == "5.6.7.8"       # 直连：头可伪造，不信


def test_global_free_cap(client, monkeypatch):
    monkeypatch.setattr(api, "FREE_TOTAL_PER_DAY", 4)
    monkeypatch.setattr(api, "check_lengths", lambda *a: None)
    ok = lambda ip: client.post("/api/screen", json={"resume_text": "x", "jds": [{"id": "1", "text": "y"}] * 3},
                                headers={"cf-connecting-ip": ip})
    monkeypatch.setattr(api, "run_streaming", lambda work, key: _empty())
    assert ok("1.1.1.1").status_code == 200
    r = ok("2.2.2.2")                                   # 全站总量 4 已用 3，再要 3 个超了
    assert r.status_code == 429 and "全站" in r.json()["detail"]


async def _empty():
    return
    yield


def test_too_long_rejected(client):
    r = client.post("/api/prep", json={"resume_text": "x" * 20_001, "jd_text": "y"})
    assert r.status_code == 400


def test_cleanup_old_checkpoints(monkeypatch):
    saver = SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False))
    monkeypatch.setattr(api, "_saver", saver)
    monkeypatch.setattr(api, "_last_cleanup", 0.0)

    class S(TypedDict):
        x: int
    g = StateGraph(S)
    g.add_node("n", lambda s: {"x": 1})
    g.add_edge(START, "n")
    g.compile(checkpointer=saver).invoke({"x": 0}, {"configurable": {"thread_id": "t"}})
    exists = lambda: bool(list(saver.list({"configurable": {"thread_id": "t"}})))

    api.cleanup_checkpoints(now=time.time() + 6 * 86400)    # 6 天后：保留
    assert exists()
    api._last_cleanup = 0.0
    api.cleanup_checkpoints(now=time.time() + 8 * 86400)    # 8 天后：删除
    assert not exists()


def test_report_event_has_structured_rewrites(client, monkeypatch, tmp_path):
    from job_agent.feedback import FeedbackStore
    monkeypatch.setattr(api, "FEEDBACK", FeedbackStore(tmp_path / "f.sqlite"))
    fake_all(monkeypatch, "电话 [PHONE_1]")   # 依据第 1～2 行，只有占位符：程序检查通过，不停在人工确认
    evs = events(client.post("/api/prep", json={"resume_text": RESUME, "jd_text": "jd"}))
    name, data = evs[-1]
    assert name == "report"
    assert data["rewrites"][0]["decision"] == "自动通过" and data["rewrites"][0]["lines"] == [1]
    assert api.FEEDBACK.stats()["rewrites"]["auto_pass"] == 1      # 自动记了一次运行
