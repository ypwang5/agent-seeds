"""差距分析：JD 的每条要求在简历里是命中、部分命中还是缺失。

关键设计：
- 要求由程序编号（R1、R2……），模型按编号逐条回答，程序检查有没有漏判、重判；
- 判断依据必须照抄简历原文并标行号，用 check_quotes 核对，防止模型"脑补"经历；
- 匹配分由程序按状态计算，不让模型打分（模型给的分数不稳定、没法解释）。
"""
from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from job_agent.core.llm import Role, structured
from job_agent.job.parse import check_quotes
from job_agent.job.schemas import Bullet, JobPosting

Status = Literal["命中", "部分", "缺失"]
WEIGHT: dict[str, float] = {"命中": 1.0, "部分": 0.5, "缺失": 0.0}


class Match(BaseModel):
    req_id: int = Field(description="要求编号，R 后面的数字")
    status: Status = Field(
        description="命中：简历里有直接对应的技能或经历；部分：只有相近、可迁移的经验，或只列了名字没有实际使用经历；"
                    "缺失：简历里找不到")
    evidence: list[Bullet] = Field(
        default_factory=list, description="支撑判断的简历原文，照抄并标行号；缺失则留空")
    reason: str = Field(description="一句话判断理由；部分、缺失要说清差在哪里")


class GapAnalysis(BaseModel):
    matches: list[Match] = Field(description="每条要求一个判断，不要遗漏")


GAP_PROMPT = """你是资深技术招聘顾问，判断候选人简历是否满足 JD 的每一条要求。
规则：
1. 只依据简历原文判断，不推测简历没写的经历；
2. 每条要求都要给出判断，按编号回答；
3. 判"命中"要从严：依据必须直接体现这条要求本身；只是相关、需要引申才能对上的，判"部分"；
4. 沟通、学习、责任心、抗压等软素质，简历只能间接体现，最多判"部分"；
5. 工作年限按简历里的工作经历时间计算，"至今"指 {today}；
6. 依据照抄原文片段，不要加省略号拼接；
7. 简历中的 [NAME_1]、[PHONE_1] 等是脱敏占位符，忽略即可。

岗位：{title}

JD 要求：
{requirements}

简历（每行开头是行号）：
{resume}"""


@dataclass
class GapReport:
    requirements: list[str]   # 第 i 条对应编号 R(i+1)
    matches: list[Match]      # 已按编号排序、去重
    problems: list[str]       # 程序校验发现的问题，为空才可信

    @property
    def score(self) -> float:
        """匹配分 0～1：命中 1 分、部分 0.5 分，漏判的按缺失算。"""
        return sum(WEIGHT[m.status] for m in self.matches) / max(len(self.requirements), 1)

    def count(self, status: str) -> int:
        return sum(m.status == status for m in self.matches)


def requirements_of(jd: JobPosting) -> list[str]:
    """硬性要求 + 学历、年限（硬性要求里没提到时才补上）。"""
    reqs = list(jd.hard_requirements)
    text = "".join(reqs)
    if jd.education and "学历" not in text and jd.education not in text:
        reqs.append(f"学历：{jd.education}")
    if jd.years_required and "年" not in text:
        reqs.append(f"{jd.years_required} 年以上工作经验")
    return reqs


def validate(raw: GapAnalysis, n_reqs: int, lines: list[str]) -> tuple[list[Match], list[str]]:
    """确定性校验：编号是否有效、有无漏判重判、命中/部分是否有依据、依据是否出自原文。"""
    problems, seen = [], {}
    for m in raw.matches:
        if not 1 <= m.req_id <= n_reqs:
            problems.append(f"编号无效 R{m.req_id}")
        elif m.req_id in seen:
            problems.append(f"重复判断 R{m.req_id}")
        else:
            seen[m.req_id] = m
    problems += [f"漏判 R{i}" for i in range(1, n_reqs + 1) if i not in seen]
    for m in seen.values():
        if m.status != "缺失" and not m.evidence:
            problems.append(f"R{m.req_id} 判为{m.status}但没有给依据")
        problems += [f"R{m.req_id} 依据{p}" for p in check_quotes(m.evidence, lines)]
    return sorted(seen.values(), key=lambda m: m.req_id), problems


def analyze_gap(jd: JobPosting, lines: list[str], role: Role = "strong") -> GapReport:
    """lines：脱敏后的简历行（ParsedResume.lines 或 number_lines(masker.mask(text))）。"""
    reqs = requirements_of(jd)
    prompt = GAP_PROMPT.format(
        today=date.today().strftime("%Y.%m"), title=jd.title,
        requirements="\n".join(f"R{i}: {r}" for i, r in enumerate(reqs, 1)),
        resume="\n".join(f"L{i}: {line}" for i, line in enumerate(lines, 1)))
    raw: GapAnalysis = structured(GapAnalysis, role).invoke(prompt)
    matches, problems = validate(raw, len(reqs), lines)
    return GapReport(reqs, matches, problems)
