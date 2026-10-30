"""风险标签：从 JD 原文和公司公开信息里找"值得警惕"的点。

两类来源，各自由程序核对：
- JD 本身（外包驻场、加班、名不副实、前后矛盾、歧视性条件）：模型给出 JD 原文片段，
  用 quote_coverage 核对确实出自 JD，对不上的丢弃；"公司未公开"是确定性规则，不用模型；
- 公司负面（近两年裁员、欠薪）：先看深度准备留下的公司调研缓存，没有再搜 1 次，
  链接必须是这次搜索真实返回过的（与 research.verify_sources 同一思路）；结果按公司名缓存 7 天。
"""
import time
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from job_agent.core.llm import Role, structured
from job_agent.core.search import SearchHit, search
from job_agent.job.parse import quote_coverage
from job_agent.job.research import CACHE_TTL, CompanyReport, _cache_path, _format

RISK_CACHE = Path(__file__).parents[3] / ".cache" / "risk"
RiskType = Literal["外包驻场", "加班强度", "名不副实", "信息矛盾", "歧视性条件", "公司未公开", "公司负面"]
# 出现就把建议降一档。公司负面只展示不降档：实测大公司几乎都查得到劳动仲裁，拿它降档会误伤（药明康德 100% 被降档）
SEVERE = {"外包驻场", "名不副实"}


class Risk(BaseModel):
    type: RiskType
    detail: str = Field(description="一句话说明风险是什么")
    source: str = Field(description="依据：JD 原文片段（一字不差照抄），或公司负面信息的来源链接")


class JdRisks(BaseModel):
    risks: list[Risk] = Field(default_factory=list)


class CompanyRisks(BaseModel):
    risks: list[Risk] = Field(default_factory=list, description="只放负面信息；没有就留空")


JD_RISK_PROMPT = """你是帮程序员避坑的求职顾问。找出下面 JD 里值得候选人警惕的地方，没有就返回空列表。
类型：
- 外包驻场：外包、驻场、派遣、人力外包，或"协助甲方正式员工"这类暗示外包的写法；
- 加班强度：明确的加班时间、大小周、单休、长期出差；
- 名不副实：标题写的方向和职责、要求明显不是一回事（如标题是 AI 应用开发，要求全是 C++ 终端开发）；
- 信息矛盾：同一份 JD 前后说法不一致（如标题 3-5 年、正文 1 年以上）；
- 歧视性条件：年龄、性别、婚育等限制。
规则：
1. source 必须一字不差照抄 JD 原文中最能说明问题的片段（不超过 40 字）；
2. 名不副实、信息矛盾可以照抄其中一处原文；
3. 招聘渠道、福利、"抗压能力强"这类常规软要求、"7×24 高可用"这类系统指标都不算风险；
4. 猎头代招、没写公司名本身不算外包驻场（程序会单独标注），必须有外包、驻场或协助甲方员工这类原文才算。

JD 原文：
{jd}"""

COMPANY_RISK_PROMPT = """下面是关于"{company}"的搜索结果。今天是 {today}。只找这家公司近两年内和员工利益直接相关的负面信息：
裁员、欠薪、拖欠工资、大规模外包用工、经营困难。监管处罚、个别员工的劳动仲裁、两年以前的旧闻都不算。
注意同名或无关公司，不确定是同一家的不要采用。
type 统一填"公司负面"，source 原样照抄链接；没有负面信息就返回空列表，不要编造。

{results}"""


def jd_risks(jd_text: str, company: str | None, role: Role = "fast") -> tuple[list[Risk], list[str]]:
    """返回（风险，校验问题）。"""
    raw: JdRisks = structured(JdRisks, role).invoke(JD_RISK_PROMPT.format(jd=jd_text))
    risks, problems = [], []
    for r in raw.risks:
        if r.type in ("公司未公开", "公司负面"):  # 这两类不归模型判断
            continue
        if quote_coverage(r.source, jd_text) < 0.8:
            problems.append(f"风险依据不在 JD 原文中，已丢弃：{r.type} {r.source[:30]}")
        elif all(x.type != r.type for x in risks):  # 同类只留一条：实测模型会把同一处矛盾换个说法报两次
            risks.append(r)
    if not company:
        risks.append(Risk(type="公司未公开", detail="JD 没写公司名，无法核实公司情况", source="（JD 中无公司名）"))
    return risks, problems


def company_risks(company: str | None, role: Role = "fast", use_cache: bool = True) -> tuple[list[Risk], list[str]]:
    if not company:
        return [], []
    # 1. 风险检查自己的缓存
    research = _cache_path(company)
    path = RISK_CACHE / research.name
    if use_cache and path.exists() and time.time() - path.stat().st_mtime < CACHE_TTL:
        return CompanyRisks.model_validate_json(path.read_text(encoding="utf-8")).risks, ["公司负面：使用缓存"]
    # 2. 深度准备做过完整调研的，拿里面的"风险信号"当素材，省一次搜索（仍要过下面同一道筛选：实测里面有 2012 年的旧闻）
    if use_cache and research.exists() and time.time() - research.stat().st_mtime < CACHE_TTL:
        rep = CompanyReport.model_validate_json(research.read_text(encoding="utf-8"))
        hits = [SearchHit(f.category, f.source_url, f.content, f.date) for f in rep.facts if f.category == "风险信号"]
    else:  # 3. 搜 1 次
        hits = search(f"{company} 裁员 欠薪 劳动纠纷")
    if not hits:
        return [], []
    seen = {h.url for h in hits}
    raw: CompanyRisks = structured(CompanyRisks, role).invoke(
        COMPANY_RISK_PROMPT.format(company=company, today=date.today().isoformat(), results=_format(hits)))
    problems = [f"来源不在搜索结果中，已丢弃：{r.detail[:30]}" for r in raw.risks if r.source not in seen]
    result = CompanyRisks(risks=[r.model_copy(update={"type": "公司负面"}) for r in raw.risks if r.source in seen])
    RISK_CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return result.risks, problems


Advice = Literal["直接投", "改简历后投", "放弃"]
LEVELS: list[Advice] = ["直接投", "改简历后投", "放弃"]


def recommend(score: float, risks: list[Risk]) -> tuple[Advice, str]:
    """程序规则给建议（可解释、稳定）：先按匹配分定档，有严重风险再降一档。"""
    level = 0 if score >= 0.75 else 1 if score >= 0.5 else 2
    reason = f"匹配分 {score:.0%}"
    severe = sorted({r.type for r in risks if r.type in SEVERE})
    if severe and level < 2:
        level += 1
        reason += f"，有严重风险（{'、'.join(severe)}）降一档"
    return LEVELS[level], reason
