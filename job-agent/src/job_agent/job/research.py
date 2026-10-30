"""公司调研：ReAct 子图（搜索 → 读结果 → 再搜），产出每条都带来源链接的事实。

关键设计：
- 搜索工具把返回过的链接记在本次调研的 seen 集合里；报告里的 source_url 必须出现在 seen 中，
  否则视为模型编造的来源，整条事实丢弃（和简历行号核对是同一思路：不信模型的自觉，由程序核对）；
- 工具调用次数用 ToolCallLimitMiddleware 封顶，防止 Agent 无限搜索烧钱；
- 结果按公司名缓存 7 天：公司公开信息不含个人数据，所有用户可以共享，省钱省时间；
- 搜索词只含公司名和岗位名，不含任何简历内容。
"""
import json
import time
from pathlib import Path
from typing import Literal

from langchain.agents import create_agent
from langchain.agents.middleware import ToolCallLimitMiddleware
from langchain.agents.structured_output import ToolStrategy
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from job_agent.core.llm import Role, get_model, model_spec
from job_agent.core.search import SearchHit, provider, search

CACHE_DIR = Path(__file__).parents[3] / ".cache" / "company"
CACHE_TTL = 7 * 24 * 3600
MAX_SEARCHES = 6

Category = Literal["业务", "规模与融资", "技术与AI", "近期动态", "风险信号", "面试经验"]


class Fact(BaseModel):
    category: Category
    content: str = Field(description="一句话事实，来自搜索结果，不要加入自己的推测")
    source_url: str = Field(description="这条事实出自哪个搜索结果的链接，必须原样照抄")
    date: str | None = Field(None, description="信息发布日期，搜索结果里有就填")


class CompanyReport(BaseModel):
    company: str
    summary: str = Field(description="两三句话概括：公司做什么、规模、和这个岗位相关的看点")
    facts: list[Fact] = Field(default_factory=list)
    not_found: list[str] = Field(default_factory=list, description="搜过但没找到可靠信息的方面")
    problems: list[str] = Field(default_factory=list, description="程序校验结果，模型不要填")


SYSTEM_PROMPT = f"""你是求职调研助手，为候选人调研一家公司，帮助他判断要不要去、面试前该了解什么。
做法：
1. 用 web_search 搜索，最多 {MAX_SEARCHES} 次。建议依次关注：主营业务、规模与融资、技术/AI 投入、近一年动态、
   裁员/欠薪/外包等风险、面试经验；
2. 只写搜索结果里出现的事实，每条都附上它来自的链接（原样照抄）；
3. 找不到的方面写进 not_found，不要编造；
4. 注意同名公司，确认是同一家再采用。"""


def _format(hits: list[SearchHit]) -> str:
    if not hits:
        return "没有搜到结果"
    return "\n\n".join(f"[{i}] {h.title}\n链接：{h.url}\n日期：{h.date or '未知'}\n{h.snippet[:400]}"
                       for i, h in enumerate(hits, 1))


def _cache_path(company: str) -> Path:
    return CACHE_DIR / f"{''.join(c for c in company if c not in '\\/:*?\"<>|')}.json"


def research_company(company: str | None, title: str = "", role: Role = "strong",
                     use_cache: bool = True) -> CompanyReport:
    if not company:
        return CompanyReport(company="未公开", summary="JD 没有写公司名（多为猎头代招），无法调研。",
                             not_found=["公司名称"])
    path = _cache_path(company)
    if use_cache and path.exists() and time.time() - path.stat().st_mtime < CACHE_TTL:
        return CompanyReport.model_validate_json(path.read_text(encoding="utf-8"))

    report = run_research(SYSTEM_PROMPT, f"调研公司：{company}；应聘岗位：{title}", CompanyReport, role,
                          "company_research", "公司名 + 业务/融资/裁员/面经")
    if report.facts:  # 一条事实都没有（多半是搜索服务出问题）就不缓存，否则会把"没查到"记住 7 天
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return report


def run_research(system_prompt: str, task: str, schema: type[BaseModel], role: Role, name: str,
                 query_hint: str) -> BaseModel:
    """通用调研骨架：ReAct Agent 搜索（次数封顶）→ 结构化输出 → 来源链接核对。
    schema 必须有 facts（每条带 source_url）和 problems 两个字段。公司调研、竞品调研共用。"""
    seen: set[str] = set()
    queries: list[str] = []

    @tool
    def web_search(query: str) -> str:
        """搜索互联网，返回标题、链接、日期和摘要。"""
        queries.append(query)
        try:
            hits = search(query)
        except Exception as e:  # 搜索失败告诉模型，让它换个词或结束，而不是整个流程崩掉
            return f"搜索失败：{type(e).__name__}"
        seen.update(h.url for h in hits)
        return _format(hits)

    web_search.description = f"搜索互联网，返回标题、链接、日期和摘要。query 用中文关键词，例如 {query_hint}。"
    agent = create_agent(
        get_model(model_spec(role)), tools=[web_search], system_prompt=system_prompt,
        response_format=ToolStrategy(schema), name=name,
        middleware=[ToolCallLimitMiddleware(tool_name="web_search", run_limit=MAX_SEARCHES)])
    result = agent.invoke({"messages": [{"role": "user", "content": task}]},
                          config={"recursion_limit": 4 * MAX_SEARCHES + 6})
    report = verify_sources(result["structured_response"], seen)
    report.problems.append(f"搜索服务：{provider()}，搜索 {len(queries)} 次（{'；'.join(queries)}），返回链接 {len(seen)} 个")
    return report


def verify_sources(report, seen: set[str]):
    """确定性校验：事实的来源链接必须是本次搜索真实返回过的，否则丢弃并记录。"""
    report.problems = [f"来源不在搜索结果中，已丢弃：{f.content[:30]}（{f.source_url[:60]}）"
                       for f in report.facts if f.source_url not in seen]
    report.facts = [f for f in report.facts if f.source_url in seen]
    return report


def report_markdown(r: CompanyReport) -> str:
    out = [f"## 公司调研：{r.company}\n", r.summary, ""]
    for cat in Category.__args__:
        facts = [f for f in r.facts if f.category == cat]
        if facts:
            out.append(f"**{cat}**")
            out += [f"- {f.content}（{f.date or '日期未知'}，[来源]({f.source_url})）" for f in facts]
            out.append("")
    if r.not_found:
        out.append("没找到：" + "、".join(r.not_found))
    return "\n".join(out)


if __name__ == "__main__":  # 手动试跑：uv run python -m job_agent.job.research 华宝证券
    import sys
    rep = research_company(sys.argv[1], use_cache=False)
    print(report_markdown(rep))
    print(json.dumps(rep.problems, ensure_ascii=False, indent=1))
