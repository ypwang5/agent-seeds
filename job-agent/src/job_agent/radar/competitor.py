"""竞品调研：给一个产品点子 / 痛点，找现有的解决方案，以及它们的定价、用户抱怨。

复用公司调研的骨架（research.run_research）：搜索次数封顶、每条事实带来源链接、链接必须是本次搜索返回过的。
给需求发现（种子 2）用：判断一个痛点"是不是已经有人做得很好了"。按话题缓存 7 天。
"""
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from job_agent.core.llm import Role
from job_agent.job.research import CACHE_TTL, MAX_SEARCHES, run_research

CACHE_DIR = Path(__file__).parents[3] / ".cache" / "competitor"
Category = Literal["产品与功能", "定价", "用户评价与抱怨", "规模与融资", "近期动态"]


class CompetitorFact(BaseModel):
    competitor: str = Field(description="这条事实属于哪个竞品")
    category: Category
    content: str = Field(description="一句话事实，来自搜索结果，不要加入自己的推测")
    source_url: str = Field(description="这条事实出自哪个搜索结果的链接，必须原样照抄")
    date: str | None = Field(None, description="信息发布日期，搜索结果里有就填")


class CompetitorReport(BaseModel):
    topic: str
    summary: str = Field(description="两三句话：现有方案有哪些、做得怎么样、还有什么没被满足")
    competitors: list[str] = Field(default_factory=list, description="找到的主要竞品名称")
    facts: list[CompetitorFact] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list, description="用户评价里反复出现、现有产品没解决好的地方；必须能在 facts 里找到依据")
    not_found: list[str] = Field(default_factory=list, description="搜过但没找到可靠信息的方面")
    problems: list[str] = Field(default_factory=list, description="程序校验结果，模型不要填")


SYSTEM_PROMPT = f"""你是产品调研助手，帮独立开发者判断一个需求方向值不值得做。
做法：
1. 用 web_search 搜索，最多 {MAX_SEARCHES} 次。先找有哪些现成产品（中英文都搜），再挑 2～4 个主要的查定价和用户评价/抱怨；
2. 只写搜索结果里出现的事实，每条附上它来自的链接（原样照抄）；
3. gaps 只写用户评价里能找到依据的不满，不要自己推测；
4. 找不到的方面写进 not_found，不要编造。"""


def research_competitors(topic: str, role: Role = "strong", use_cache: bool = True) -> CompetitorReport:
    path = CACHE_DIR / f"{''.join(c for c in topic if c not in '\\/:*?\"<>|')[:60]}.json"
    if use_cache and path.exists() and time.time() - path.stat().st_mtime < CACHE_TTL:
        return CompetitorReport.model_validate_json(path.read_text(encoding="utf-8"))
    report = run_research(SYSTEM_PROMPT, f"调研这个需求方向的现有竞品：{topic}", CompetitorReport, role,
                          "competitor_research", "产品类别 + 推荐/对比/定价/吐槽，或英文 best X app / X alternatives")
    report.topic = topic
    if report.facts:  # 一条事实都没有（多半是搜索服务出问题）就不缓存，否则会把"没查到"记住 7 天
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return report


def report_markdown(r: CompetitorReport) -> str:
    out = [f"## 竞品调研：{r.topic}\n", r.summary, ""]
    if r.competitors:
        out.append("主要竞品：" + "、".join(r.competitors) + "\n")
    for cat in Category.__args__:
        facts = [f for f in r.facts if f.category == cat]
        if facts:
            out.append(f"**{cat}**")
            out += [f"- {f.competitor}：{f.content}（[来源]({f.source_url})）" for f in facts]
            out.append("")
    if r.gaps:
        out += ["**现有产品没解决好的地方**"] + [f"- {g}" for g in r.gaps] + [""]
    if r.not_found:
        out.append("没找到：" + "、".join(r.not_found))
    return "\n".join(out)


if __name__ == "__main__":  # 手动试跑：uv run python -m job_agent.radar.competitor 记账
    import sys
    rep = research_competitors(sys.argv[1], use_cache=False)
    print(report_markdown(rep))
    print(rep.problems)
