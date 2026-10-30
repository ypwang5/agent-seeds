"""离线测试：不调用模型。"""
from job_agent.core.pii import Masker
from job_agent.job.parse import check_citations, number_lines
from job_agent.job.schemas import Bullet

RESUME = """姓名：张小明  手机：13812345678
Email：xm.zhang@example.com  身份证：11010519900307123X
GitHub：github.com/xm
负责仓储管理 SOA 接口改造为微服务架构"""


def test_mask_removes_pii_and_unmask_restores():
    m = Masker()
    masked = m.mask(RESUME)
    for secret in ["张小明", "13812345678", "xm.zhang@example.com", "11010519900307123X"]:
        assert secret not in masked
    assert "[PHONE_1]" in masked and "[NAME_1]" in masked
    assert "github.com/xm" in masked  # 非敏感信息保留
    assert m.unmask(masked) == RESUME


def test_mask_same_value_same_placeholder():
    m = Masker(names=["张小明"])
    assert m.mask("张小明 张小明") == "[NAME_1] [NAME_1]"


def test_id_card_not_mistaken_for_phone():
    masked = Masker().mask("身份证 11010519900307123X")
    assert "[ID_1]" in masked and "PHONE" not in masked


def test_check_citations():
    lines = number_lines("\n\n第一行：负责贷后模块开发\n  第二行：主导信创改造  \n")
    assert lines == ["第一行：负责贷后模块开发", "第二行：主导信创改造"]
    ok = Bullet(text="负责贷后模块开发", lines=[1])
    bad_line = Bullet(text="负责贷后模块开发", lines=[9])
    made_up = Bullet(text="带领 10 人团队完成大模型平台建设", lines=[2])
    problems = check_citations([ok, bad_line, made_up], lines)
    assert len(problems) == 2
    assert "行号无效" in problems[0] and "与原文不符" in problems[1]


def test_parse_jd_drops_signals_not_in_source(monkeypatch):
    from types import SimpleNamespace

    from job_agent.job import parse
    from job_agent.job.schemas import JobPosting

    fake = JobPosting(title="Java", employment_signals=["项目驻场 某银行", "经常出差"])  # 第二条是编的
    monkeypatch.setattr(parse, "structured", lambda *a, **k: SimpleNamespace(invoke=lambda _: fake))
    jd = parse.parse_jd("Java 开发（项目驻场某银行）")
    assert jd.employment_signals == ["项目驻场 某银行"]  # 忽略空白差异后能在原文找到


def test_fallback_only_for_server_key(monkeypatch):
    from job_agent.core import llm
    monkeypatch.setenv("DASHSCOPE_API_KEY", "x")
    monkeypatch.delenv("JOB_FALLBACK_MODEL", raising=False)
    assert llm.fallback_spec("deepseek:deepseek-flash") == "qwen:qwen3.7-plus"
    assert llm.fallback_spec("qwen:qwen3.7-plus") is None          # 自己不给自己兜底
    token = llm.USER_KEY.set("sk-user")
    try:
        assert llm.fallback_spec("deepseek:deepseek-flash") is None  # 用户自带 Key：不花服务器的通义 Key
    finally:
        llm.USER_KEY.reset(token)
