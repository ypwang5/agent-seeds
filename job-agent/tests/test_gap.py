"""差距分析的离线测试：不调用模型，只测程序校验和计分。"""
from job_agent.job.gap import GapAnalysis, GapReport, Match, requirements_of, validate
from job_agent.job.schemas import Bullet, JobPosting

LINES = ["2019.07-2026.07 某银行 Java 开发", "负责贷后模块开发，使用 Spring Boot", "用 LangChain 做过 RAG 问答 demo"]


def ev(text: str, line: int) -> list[Bullet]:
    return [Bullet(text=text, lines=[line])]


def test_validate_catches_missing_duplicate_and_bad_ids():
    raw = GapAnalysis(matches=[
        Match(req_id=1, status="命中", evidence=ev("使用 Spring Boot", 2), reason="有"),
        Match(req_id=1, status="缺失", reason="重复"),
        Match(req_id=9, status="缺失", reason="编号越界"),
    ])
    matches, problems = validate(raw, n_reqs=3, lines=LINES)
    assert [m.req_id for m in matches] == [1]
    assert matches[0].status == "命中"  # 重复时保留第一个
    assert "重复判断 R1" in problems and "编号无效 R9" in problems
    assert "漏判 R2" in problems and "漏判 R3" in problems


def test_validate_requires_real_evidence():
    raw = GapAnalysis(matches=[
        Match(req_id=1, status="部分", reason="没给依据"),
        Match(req_id=2, status="命中", evidence=ev("主导过千万级用户的大模型平台建设", 3), reason="编的"),
        Match(req_id=3, status="缺失", reason="缺失不需要依据"),
    ])
    _, problems = validate(raw, n_reqs=3, lines=LINES)
    assert len(problems) == 2
    assert "R1 判为部分但没有给依据" in problems[0]
    assert problems[1].startswith("R2 依据与原文不符")


def test_check_quotes_accepts_excerpts_rejects_made_up():
    from job_agent.job.parse import check_quotes
    lines = ["2021.09 – 2026.07 某某系统工程股份有限公司 Java 开发工程师", "驻场某物流集团，参与物流调度系统研发",
             "处理跨省及加急两类运单数据，日均百万笔"]
    ok_excerpt = Bullet(text="物流调度系统…跨省及加急两类运单数据", lines=[2, 3])  # 跨行 + 省略号
    ok_partial = Bullet(text="某某系统工程股份有限公司 Java 开发", lines=[1])
    made_up = Bullet(text="主导千万级用户大模型平台架构设计", lines=[2])
    assert check_quotes([ok_excerpt, ok_partial], lines) == []
    assert len(check_quotes([made_up], lines)) == 1


def test_score_counts_unjudged_as_missing():
    report = GapReport(
        requirements=["a", "b", "c", "d"],
        matches=[Match(req_id=1, status="命中", reason=""), Match(req_id=2, status="部分", reason="")],
        problems=["漏判 R3", "漏判 R4"])
    assert report.score == 1.5 / 4
    assert report.count("命中") == 1 and report.count("缺失") == 0


def test_requirements_add_education_and_years_only_when_absent():
    jd = JobPosting(title="x", education="本科", years_required=3, hard_requirements=["熟悉 Python"])
    assert requirements_of(jd) == ["熟悉 Python", "学历：本科", "3 年以上工作经验"]
    jd2 = JobPosting(title="x", education="本科", years_required=3,
                     hard_requirements=["本科及以上学历", "3 年以上 Python 经验"])
    assert requirements_of(jd2) == jd2.hard_requirements
