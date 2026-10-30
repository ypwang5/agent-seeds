"""深度准备主图（LangGraph）。

    解析 JD ─┬→ 差距分析 ─┬→ 简历改写+校验 ─有问题→ 人工确认(interrupt) ─┐
             └→ 公司调研 ─┘               └─全部通过──────────────────────┴→ 面试题 → 汇总报告

设计要点：
- 差距分析和公司调研互不依赖，从同一个节点分出两条边，LangGraph 会在同一步里并行执行，
  再用 add_edge([a, b], c) 等两者都完成才进入改写；
- 状态里只放 JSON 友好的数据（dict / list / str），checkpointer 存档时不会遇到序列化问题；
- 状态里没有个人信息：简历在进图之前已经脱敏，占位符映射只在调用方内存里，最后由调用方还原；
- 人工确认节点在 interrupt 之前没有任何副作用（恢复时这个节点会从头重跑）。
"""
import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from job_agent.job.gap import GapReport, Match, analyze_gap
from job_agent.job.interview import Question, generate_questions
from job_agent.job.parse import parse_jd
from job_agent.job.research import CompanyReport, report_markdown, research_company
from job_agent.job.rewrite import RewriteItem, check_items, rewrite_resume
from job_agent.job.schemas import JobPosting


class PrepState(TypedDict, total=False):
    jd_text: str                     # 输入：JD 原文
    lines: list[str]                 # 输入：脱敏后的简历行
    jd: dict
    gap: dict
    company: dict | None
    rewrites: list[dict]             # 每条改写的校验结果，人工确认后补上 final_text / decision
    questions: list[dict]
    problems: Annotated[list[str], operator.add]  # 各节点的校验问题，并行节点各自追加，用 add 合并
    report_md: str


def _gap(state: PrepState) -> GapReport:
    g = state["gap"]
    return GapReport(g["requirements"], [Match(**m) for m in g["matches"]], g["problems"])


def node_parse(state: PrepState) -> dict:
    return {"jd": parse_jd(state["jd_text"]).model_dump()}


def node_gap(state: PrepState) -> dict:
    report = analyze_gap(JobPosting(**state["jd"]), state["lines"])
    return {"gap": {"requirements": report.requirements, "matches": [m.model_dump() for m in report.matches],
                    "problems": report.problems},
            "problems": [f"差距分析：{p}" for p in report.problems]}


def node_research(state: PrepState) -> dict:
    jd = state["jd"]
    try:
        report = research_company(jd.get("company"), jd.get("title", ""))
    except Exception as e:  # 搜索服务挂了不影响后面的改写和出题
        return {"company": None, "problems": [f"公司调研失败：{type(e).__name__}: {e}"]}
    return {"company": report.model_dump(), "problems": [f"公司调研：{p}" for p in report.problems]}


def node_rewrite(state: PrepState) -> dict:
    items = rewrite_resume(_gap(state), state["jd"]["title"], state["lines"])
    results = check_items(items, state["lines"])
    for r in results:
        if not r["flagged"]:
            r["final_text"], r["decision"] = r["item"]["new_text"], "自动通过"
    return {"rewrites": results}


def route_after_rewrite(state: PrepState) -> str:
    return "review" if any(r["flagged"] for r in state["rewrites"]) else "interview"


def node_review(state: PrepState) -> dict:
    """把校验没通过的改写一次性交给人。恢复值格式：{"编号": {"action": "accept|edit|reject", "text": "..."}}。"""
    flagged = [(i, r) for i, r in enumerate(state["rewrites"]) if r["flagged"]]
    answers = interrupt({"type": "review_rewrites", "items": [
        {"index": i, "original": r["item"]["original"], "new_text": r["item"]["new_text"], "source": r["source"],
         "rule_problems": r["rule_problems"], "verdict": r["verdict"], "reason": r["reason"]} for i, r in flagged]})
    rewrites = [dict(r) for r in state["rewrites"]]
    for i, r in flagged:
        ans = answers.get(str(i)) or {"action": "reject"}  # 没回答的按拒绝处理：宁可不改，也不留编造
        if ans["action"] == "accept":
            r_new = {"final_text": r["item"]["new_text"], "decision": "人工确认采纳"}
        elif ans["action"] == "edit":
            r_new = {"final_text": ans["text"], "decision": "人工修改"}
        else:
            r_new = {"final_text": r["item"]["original"], "decision": "已拒绝，保留原文"}
        rewrites[i] = {**r, **r_new}
    return {"rewrites": rewrites}


def node_interview(state: PrepState) -> dict:
    company = CompanyReport(**state["company"]) if state.get("company") else None
    questions, problems = generate_questions(JobPosting(**state["jd"]), _gap(state), state["lines"], company)
    return {"questions": [q.model_dump() for q in questions], "problems": [f"面试题：{p}" for p in problems]}


def node_report(state: PrepState) -> dict:
    return {"report_md": render_report(state)}


def build_graph(checkpointer=None):
    g = StateGraph(PrepState)
    for name, fn in [("parse", node_parse), ("gap", node_gap), ("research", node_research),
                     ("rewrite", node_rewrite), ("review", node_review), ("interview", node_interview),
                     ("report", node_report)]:
        g.add_node(name, fn)
    g.add_edge(START, "parse")
    g.add_edge("parse", "gap")
    g.add_edge("parse", "research")
    g.add_edge(["gap", "research"], "rewrite")   # 两个并行分支都完成后才改写
    g.add_conditional_edges("rewrite", route_after_rewrite, ["review", "interview"])
    g.add_edge("review", "interview")
    g.add_edge("interview", "report")
    g.add_edge("report", END)
    return g.compile(checkpointer=checkpointer)


def _cell(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", "<br>")


def render_report(state: PrepState) -> str:
    jd, gap = state["jd"], _gap(state)
    out = [f"# 面试准备：{jd['title']}（{jd.get('company') or '公司未公开'}）\n",
           f"## 一、匹配情况\n\n匹配分 **{gap.score:.0%}**（命中 {gap.count('命中')} / 部分 {gap.count('部分')} / "
           f"缺失 {gap.count('缺失')}）\n", "| # | 要求 | 判断 | 理由 |", "|---|---|---|---|"]
    out += [f"| R{m.req_id} | {_cell(gap.requirements[m.req_id - 1])} | {m.status} | {_cell(m.reason)} |"
            for m in gap.matches]
    company = CompanyReport(**state["company"]) if state.get("company") else None
    out += ["", report_markdown(company).replace("## 公司调研", "## 二、公司调研") if company
            else "## 二、公司调研\n\n调研失败或未进行，见文末问题清单。", "",
            "## 三、简历改写建议\n", "| 原文 | 建议改为 | 处理 | 校验 |", "|---|---|---|---|"]
    for r in state.get("rewrites", []):
        check = "；".join(r["rule_problems"] + ([f"复核：{r['verdict']}（{r['reason']}）"] if r["verdict"] != "通过" else []))
        out.append(f"| {_cell(r['item']['original'])} | {_cell(r.get('final_text', ''))} | {r.get('decision', '')} "
                   f"| {_cell(check) or '通过'} |")
    out.append("\n## 四、面试题\n")
    for i, q in enumerate(map(lambda d: Question(**d), state.get("questions", [])), 1):
        out.append(f"### {i}.【{q.category}】{q.question}\n\n考察点：{q.intent}\n")
        if q.follow_ups:
            out += ["追问："] + [f"{k}. {f}" for k, f in enumerate(q.follow_ups, 1)] + [""]
        out += ["答题要点："] + [f"- {p}" for p in q.answer_points]
        if q.evidence:
            out.append("- 依据：" + "；".join(f"L{','.join(map(str, b.lines))}「{b.text}」" for b in q.evidence))
        out.append("")
    if state.get("problems"):
        out += ["## 附：校验记录\n"] + [f"- {p}" for p in state["problems"]]
    return "\n".join(out) + "\n"


__all__ = ["build_graph", "PrepState", "RewriteItem"]
