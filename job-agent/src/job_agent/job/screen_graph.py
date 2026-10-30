"""岗位筛选图（LangGraph map-reduce）：一份简历 × 多个 JD，每个 JD 并行处理，最后汇总排序。

    START ─Send×N→ screen_one（解析 JD → 匹配打分 → 风险检查 → 建议）─→ rank → END

设计要点：
- Send：条件边返回 [Send("screen_one", 子输入), ...]，有几个 JD 就开几个并行分支；
  每个分支拿到的是自己的子输入（不是整份状态），结果写进 results，用 operator.add 合并；
- 单个 JD 失败不影响其他 JD：screen_one 内部兜底，失败的记 error；
- 进度：节点内用 get_stream_writer() 发自定义事件，调用方用 stream_mode="custom" 实时收到，
  网页端据此显示"第 3 个 JD 正在查风险"；
- 筛选量大，全部用快模型；公司负面信息按公司名缓存，多个用户、多次筛选共享。
"""
import operator
from typing import Annotated, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from job_agent.job.gap import analyze_gap
from job_agent.job.parse import parse_jd
from job_agent.job.risk import company_risks, jd_risks, recommend

ORDER = {"直接投": 0, "改简历后投": 1, "放弃": 2}


class ScreenState(TypedDict, total=False):
    lines: list[str]                              # 输入：脱敏后的简历行
    jds: list[dict]                               # 输入：[{"id": "...", "text": "JD 原文"}]
    results: Annotated[list[dict], operator.add]  # 每个分支追加一条
    ranked: list[dict]


class OneJob(TypedDict):
    lines: list[str]
    jd: dict


def fan_out(state: ScreenState) -> list[Send]:
    return [Send("screen_one", {"lines": state["lines"], "jd": jd}) for jd in state["jds"]]


def screen_one(job: OneJob) -> dict:
    jd_id, text = job["jd"]["id"], job["jd"]["text"]
    emit = get_stream_writer()
    emit({"id": jd_id, "step": "解析 JD"})
    try:
        jd = parse_jd(text)
        emit({"id": jd_id, "step": "匹配打分", "title": jd.title, "company": jd.company})
        gap = analyze_gap(jd, job["lines"], role="fast")
        emit({"id": jd_id, "step": "风险检查"})
        risks, problems = jd_risks(text, jd.company)
        try:
            c_risks, c_problems = company_risks(jd.company)
        except Exception as e:  # 搜索挂了只少一类风险，不影响打分
            c_risks, c_problems = [], [f"公司负面信息查询失败：{type(e).__name__}"]
        risks += c_risks
        advice, reason = recommend(gap.score, risks)
    except Exception as e:
        emit({"id": jd_id, "step": "失败"})
        return {"results": [{"id": jd_id, "error": f"{type(e).__name__}: {e}"}]}
    emit({"id": jd_id, "step": "完成"})
    return {"results": [{
        "id": jd_id, "title": jd.title, "company": jd.company, "city": jd.city, "salary": jd.salary,
        "score": round(gap.score, 3), "advice": advice, "advice_reason": reason,
        "counts": {s: gap.count(s) for s in ("命中", "部分", "缺失")},
        "matches": [{"requirement": gap.requirements[m.req_id - 1], **m.model_dump()} for m in gap.matches],
        "risks": [r.model_dump() for r in risks],
        "problems": gap.problems + problems + c_problems,
    }]}


def rank(state: ScreenState) -> dict:
    ok = [r for r in state["results"] if "error" not in r]
    failed = [r for r in state["results"] if "error" in r]
    ok.sort(key=lambda r: (ORDER[r["advice"]], -r["score"]))
    return {"ranked": ok + failed}


def build_graph():
    g = StateGraph(ScreenState)
    g.add_node("screen_one", screen_one)
    g.add_node("rank", rank)
    g.add_conditional_edges(START, fan_out, ["screen_one"])
    g.add_edge("screen_one", "rank")
    g.add_edge("rank", END)
    return g.compile()
