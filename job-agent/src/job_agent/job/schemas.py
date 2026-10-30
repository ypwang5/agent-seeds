"""JD 和简历的结构化数据。Field 的 description 会作为说明发给模型，所以写清楚抽取规则。"""
from pydantic import BaseModel, Field


class JobPosting(BaseModel):
    title: str = Field(description="岗位名称")
    company: str | None = Field(None, description="招聘公司名；JD 没写则为 null")
    city: str | None = Field(None, description="工作城市")
    salary: str | None = Field(None, description="薪资原文，如 25-40K·15薪")
    years_required: int | None = Field(None, description="要求的最低工作年限（数字）；没写则为 null")
    education: str | None = Field(None, description="学历要求，如 本科")
    responsibilities: list[str] = Field(default_factory=list, description="岗位职责，每条一句")
    hard_requirements: list[str] = Field(
        default_factory=list, description="硬性要求（必须满足的），每条一个独立要求；'熟悉A和B'拆成两条")
    nice_to_have: list[str] = Field(default_factory=list, description="加分项、优先条件")
    tech_keywords: list[str] = Field(
        default_factory=list,
        description="技术关键词：只写具体的编程语言、框架、工具、中间件、技术名词，用通用写法，如 Python、LangGraph、RAG、"
                    "Kubernetes；能力描述（如 性能调优、数据结构、系统设计）不算")
    employment_signals: list[str] = Field(
        default_factory=list,
        description="只摘录 JD 原文中描述'用工方式'（外包、驻场、派遣）或'工作强度'（加班、单双休、出差）的短语，"
                    "必须一字不差出自原文；招聘人数、招聘渠道（如猎头发布）、公司信息、工具福利、岗位职责都不算；"
                    "原文没有就留空列表")


class Bullet(BaseModel):
    text: str = Field(description="这一条的内容，照抄原文，不改写、不总结")
    lines: list[int] = Field(description="这一条来自原文的哪几行（行号 L 后面的数字）")


class Experience(BaseModel):
    company: str
    role: str | None = None
    start: str | None = Field(None, description="开始时间，格式 YYYY.MM")
    end: str | None = Field(None, description="结束时间，格式 YYYY.MM；至今则写 至今")
    bullets: list[Bullet] = Field(default_factory=list, description="这段经历下的职责、项目、成果")


class Project(BaseModel):
    name: str
    start: str | None = None
    end: str | None = None
    tech: list[str] = Field(default_factory=list, description="用到的技术")
    bullets: list[Bullet] = Field(default_factory=list)


class Resume(BaseModel):
    skills: list[Bullet] = Field(default_factory=list, description="专业技能部分，每条技能描述一条")
    experiences: list[Experience] = Field(default_factory=list, description="工作经历（公司维度）")
    projects: list[Project] = Field(default_factory=list, description="项目经历")
    education: list[str] = Field(default_factory=list, description="教育经历，每段一条")

    def all_bullets(self) -> list[Bullet]:
        return self.skills + [b for e in self.experiences for b in e.bullets] + \
            [b for p in self.projects for b in p.bullets]
