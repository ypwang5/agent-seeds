"""面试题生成：岗位技术题、简历项目深挖（3 层追问）、公司相关题，每题附结合简历的答题要点。

答题要点引用的简历原文同样用 check_quotes 核对，避免模型替候选人"编"经历来答题。
"""
from typing import Literal

from pydantic import BaseModel, Field

from job_agent.core.llm import Role, structured
from job_agent.job.gap import GapReport
from job_agent.job.parse import check_quotes
from job_agent.job.research import CompanyReport
from job_agent.job.schemas import Bullet, JobPosting


class Question(BaseModel):
    category: Literal["技术", "项目深挖", "公司"]
    question: str
    intent: str = Field(description="面试官想考察什么")
    follow_ups: list[str] = Field(default_factory=list, description="项目深挖题给出 3 层递进追问，其他类可留空")
    answer_points: list[str] = Field(description="答题要点，结合候选人的真实经历；简历没有的经验要提示'如实说明没做过'")
    evidence: list[Bullet] = Field(default_factory=list, description="答题要点引用的简历原文，照抄并标行号")


class QuestionSet(BaseModel):
    questions: list[Question]


PROMPT = """你是这个岗位的技术面试官，同时也是候选人的面试教练。出一套面试准备题：
- 技术题 {n_tech} 道：围绕 JD 的核心要求，优先考候选人"部分"或"缺失"的方向；
- 项目深挖 {n_proj} 道：挑简历里和岗位最相关的项目，每道给 3 层递进追问（做了什么 → 为什么这样做 → 换个条件怎么办）；
- 公司相关 {n_company} 道：结合公司调研（没有调研信息就出"为什么选择我们""对行业的理解"这类通用题）。
答题要点必须基于简历真实经历，引用原文并标行号；简历没有的经验，要点里写"如实说明没做过，讲清楚学习路径"，不要替候选人编。
[NAME_1] 等是脱敏占位符。

岗位：{title}
JD 要求及匹配情况：
{gaps}

公司调研摘要：
{company}

简历（每行开头是行号）：
{resume}"""


def generate_questions(jd: JobPosting, gap: GapReport, lines: list[str], company: CompanyReport | None,
                       role: Role = "strong", n_tech: int = 4, n_proj: int = 3, n_company: int = 2
                       ) -> tuple[list[Question], list[str]]:
    gaps = "\n".join(f"R{m.req_id}（{m.status}）: {gap.requirements[m.req_id - 1]}" for m in gap.matches)
    company_text = "无" if not company or not company.facts else \
        company.summary + "\n" + "\n".join(f"- [{f.category}] {f.content}" for f in company.facts)
    prompt = PROMPT.format(n_tech=n_tech, n_proj=n_proj, n_company=n_company, title=jd.title, gaps=gaps,
                           company=company_text,
                           resume="\n".join(f"L{i}: {line}" for i, line in enumerate(lines, 1)))
    questions = structured(QuestionSet, role).invoke(prompt).questions
    problems = [f"第 {i} 题依据{p}" for i, q in enumerate(questions, 1) for p in check_quotes(q.evidence, lines)]
    return questions, problems
