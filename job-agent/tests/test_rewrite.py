"""防编造第一层（程序检查）的离线测试。"""
from job_agent.job.rewrite import RewriteItem, rule_check, source_text

SRC = "参与物流调度系统研发，处理跨省及加急运单数据，使用 SpringBoot 与 DB2，日均处理 120 万笔"


def test_honest_rewrites_pass():
    assert rule_check("参与物流调度系统研发：基于 SpringBoot、DB2 处理跨省与加急运单数据（日均 120 万笔）", SRC) == []
    assert rule_check("在物流调度系统中参与运单数据处理模块研发", SRC) == []


def test_changed_number_flagged():
    assert rule_check("参与物流调度系统研发，日均处理 500 万笔运单", SRC) == ["数字 500 在原文中没有"]


def test_new_tech_flagged():
    problems = rule_check("参与物流调度系统研发，使用 SpringBoot、Kafka 处理运单数据", SRC)
    assert problems == ["技术名 Kafka 在原文中没有"]


def test_role_escalation_flagged():
    problems = rule_check("主导物流调度系统研发，处理跨省及加急运单数据", SRC)
    assert problems == ["疑似拔高：原文没有'主导'"]


def test_placeholders_ignored_and_bad_lines():
    assert rule_check("[NAME_1] 参与物流调度系统研发", SRC) == []
    assert rule_check("随便写", "") == ["依据行号无效"]


def test_source_text_tolerates_pdf_line_breaks():
    lines = ["a", "b", "c", "d"]
    assert source_text(lines, [2]) == "abc"
    assert source_text(lines, [9]) == ""


def test_synonyms_not_flagged():
    assert rule_check("LLM 工程化：SSE 流式输出", "大模型工程化：SSE 流式输出") == []
    assert rule_check("Kafka 工程化", "大模型工程化") == ["技术名 Kafka 在原文中没有"]


def test_hint_when_term_exists_elsewhere():
    full = SRC + "使用 Kafka 做异步解耦"
    assert rule_check("参与物流调度系统研发，使用 Kafka", SRC, full) == ["技术名 Kafka 在原文中没有（简历其他位置有，属于跨条目拼接）"]


def test_rewrite_drops_noop_and_duplicate_original(monkeypatch):
    from job_agent.job import rewrite
    from job_agent.job.gap import GapReport
    items = [RewriteItem(lines=[1], original="A 原文", new_text="A 原文", change=""),        # 原样返回
             RewriteItem(lines=[2], original="B 原文", new_text="B 改法一", change=""),
             RewriteItem(lines=[2], original="B 原文", new_text="B 改法二", change="")]     # 同一条原文第二个版本
    fake = type("M", (), {"invoke": lambda self, p: rewrite.Rewrite(items=items)})()
    monkeypatch.setattr(rewrite, "structured", lambda schema, role: fake)
    out = rewrite.rewrite_resume(GapReport([], [], []), "岗位", ["A 原文", "B 原文"])
    assert [it.new_text for it in out] == ["B 改法一"]


def test_new_phrase_detected_but_reorder_is_not():
    src = "熟练使用 Claude Code 等 AI 编程工具；Python（FastAPI、pytest）"
    assert rule_check("熟练使用 Claude Code 等 AI 编程工具，并检查生成代码的安全", src) == [
        "新增表述：'并检查生成代码的安全' 原文没有，请确认属实"]
    assert rule_check("Python（FastAPI、pytest）；熟练使用 Claude Code 等 AI 编程工具", src) == []   # 只调语序


def test_cn_terms_flagged():
    assert rule_check("负责订单服务后端微服务", "负责订单服务后端") == ["新增技术/能力词：原文没有'微服务'"]


def test_dropped_role_qualifier_flagged():
    src = "AI 文案助手：参与前端开发：基于 Vue3 实现对话界面"
    assert rule_check("参与 AI 应用开发：基于 Vue3 实现对话界面", src) == [
        "去掉了限定词'前端'：原文是前端开发，改写可能扩大了职责范围"]
    assert rule_check("参与前端开发：用 Vue3 实现对话界面", src) == []
