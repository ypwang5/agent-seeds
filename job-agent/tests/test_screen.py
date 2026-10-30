"""岗位筛选的离线测试：风险依据核对、建议规则、Send 并行与失败隔离。"""
from job_agent.job import risk, screen_graph as sg
from job_agent.job.gap import GapReport, Match
from job_agent.job.risk import JdRisks, Risk, recommend
from job_agent.job.schemas import JobPosting

JD = "岗位：AI 应用开发。要求：熟悉 Python。工作地点：客户现场驻场办公，周内加班到20：30。"


class FakeModel:
    def __init__(self, out):
        self.out = out

    def invoke(self, prompt):
        return self.out


def test_jd_risk_quote_must_come_from_jd(monkeypatch):
    monkeypatch.setattr(risk, "structured", lambda schema, role="fast": FakeModel(JdRisks(risks=[
        Risk(type="外包驻场", detail="驻场", source="客户现场驻场办公"),
        Risk(type="加班强度", detail="996", source="实行996工作制"),         # JD 里没有，应丢弃
        Risk(type="公司负面", detail="模型越权判断", source="客户现场驻场办公"),  # 不归模型判断，应忽略
    ])))
    risks, problems = risk.jd_risks(JD, company=None)
    assert [r.type for r in risks] == ["外包驻场", "公司未公开"]
    assert problems == ["风险依据不在 JD 原文中，已丢弃：加班强度 实行996工作制"]


def test_recommend_rules():
    assert recommend(0.8, []) == ("直接投", "匹配分 80%")
    assert recommend(0.6, [])[0] == "改简历后投"
    assert recommend(0.3, [])[0] == "放弃"
    out = Risk(type="外包驻场", detail="", source="")
    assert recommend(0.8, [out]) == ("改简历后投", "匹配分 80%，有严重风险（外包驻场）降一档")
    assert recommend(0.8, [Risk(type="加班强度", detail="", source="")])[0] == "直接投"  # 非严重风险不降档


def test_screen_runs_in_parallel_and_isolates_failure(monkeypatch):
    def fake_parse(text):
        if "坏" in text:
            raise ValueError("解析失败")
        return JobPosting(title=text, company="某公司", hard_requirements=["a", "b"])

    scores = {"好岗位": ["命中", "命中"], "一般岗位": ["命中", "缺失"]}
    monkeypatch.setattr(sg, "parse_jd", fake_parse)
    monkeypatch.setattr(sg, "analyze_gap", lambda jd, lines, role: GapReport(
        ["a", "b"], [Match(req_id=i, status=s, reason="") for i, s in enumerate(scores[jd.title], 1)], []))
    monkeypatch.setattr(sg, "jd_risks", lambda text, company: ([], []))
    monkeypatch.setattr(sg, "company_risks", lambda company: ([], []))

    jds = [{"id": "1", "text": "一般岗位"}, {"id": "2", "text": "坏"}, {"id": "3", "text": "好岗位"}]
    events, final = [], None
    for mode, chunk in sg.build_graph().stream({"lines": ["x"], "jds": jds}, stream_mode=["custom", "values"]):
        if mode == "custom":
            events.append(chunk)
        else:
            final = chunk
    ranked = final["ranked"]
    assert [r["id"] for r in ranked] == ["3", "1", "2"]   # 直接投(100%) > 改简历后投(50%) > 失败的排最后
    assert ranked[1]["advice"] == "改简历后投" and "error" in ranked[2]
    assert {e["id"] for e in events if e["step"] == "完成"} == {"1", "3"}
    assert {"id": "2", "step": "失败"} in events


def test_same_type_risk_kept_once(monkeypatch):
    monkeypatch.setattr(risk, "structured", lambda schema, role="fast": FakeModel(JdRisks(risks=[
        Risk(type="外包驻场", detail="驻场", source="客户现场驻场办公"),
        Risk(type="外包驻场", detail="同一处换个说法", source="驻场办公"),
    ])))
    risks, _ = risk.jd_risks(JD, company="某公司")
    assert [r.detail for r in risks] == ["驻场"]
