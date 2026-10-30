"""简历改写 + 防编造校验。

改写原则：只换说法、调整侧重点，让已有经历更贴近 JD 的表述；不新增事实。
校验分两层，任何一层报问题，这条改写就交给人工确认：
1. 程序检查（确定性、可测试）：改写里的数字、英文技术名必须出现在依据原文中；
   "主导、带领"等表示角色升级的词，原文没有而改写有，标为疑似夸大；
2. 模型复核：逐条判断有没有夸大或编造（程序抓不到的语义问题，如"了解"写成"精通"）。
"""
import re
from typing import Literal

from pydantic import BaseModel, Field

from job_agent.core.llm import Role, structured
from job_agent.job.gap import GapReport

# 原文没有、改写里出现就可能是角色升级
ESCALATION_WORDS = ["主导", "牵头", "带领", "带队", "负责人", "独立负责", "从0到1", "从 0 到 1", "架构设计", "精通", "全面负责"]
NUMBER = re.compile(r"\d+(?:\.\d+)?")
PLACEHOLDER = re.compile(r"\[[A-Z]+_\d+\]")
# 英文技术名的中文同义说法：原文写中文、改写换成英文缩写不算新增（实测"大模型"→"LLM"被误报）
SYNONYMS = {"llm": ["大模型", "大语言模型"], "ai": ["人工智能", "大模型", "智能"], "api": ["接口"],
            "agent": ["智能体"], "rag": ["检索增强"]}
TECH = re.compile(r"[A-Za-z][A-Za-z0-9+#.\-]*[A-Za-z0-9+#]|[A-Za-z]")
# 中文的技术 / 能力词：原文没有、改写有就标出来（实测"后端"→"后端微服务"、"了解 LangGraph"→"多智能体编排"漏网）
CN_TERMS = ["微服务", "分布式", "高并发", "多智能体", "原理", "中台", "大数据", "全链路", "可复用"]
# 岗位限定词：原文"前端开发"、改写只剩"开发"，等于把职责范围扩大了（实测"参与前端开发"→"参与 AI 应用开发"三轮都漏网）
QUALIFIERS = ["前端", "后端", "测试", "运维", "客户端", "移动端", "嵌入式", "数据"]
NEW_PHRASE_MIN = 4  # 连续几个中文字在原文里没依据才算"新增表述"（第 4 周用 39 条人工判断过的改写定的阈值）


class RewriteItem(BaseModel):
    lines: list[int] = Field(description="依据的简历原文行号（被改写的那一条所在的行）")
    original: str = Field(description="被改写的原文，照抄")
    new_text: str = Field(description="改写后的内容")
    req_ids: list[int] = Field(default_factory=list, description="这条改写是为了贴近哪几条 JD 要求（R 编号）")
    change: str = Field(description="一句话说明改了什么、为什么")


class Rewrite(BaseModel):
    items: list[RewriteItem] = Field(default_factory=list)


class Verdict(BaseModel):
    index: int = Field(description="改写编号")
    verdict: Literal["通过", "夸大", "编造"] = Field(
        description="通过：内容都能在原文找到依据；夸大：事实有依据但程度、角色、范围被拔高；编造：出现原文没有的事实")
    reason: str = Field(description="一句话理由；通过可简写")


class Review(BaseModel):
    verdicts: list[Verdict]


REWRITE_PROMPT = """你是简历优化顾问。候选人要投递下面的岗位，请改写简历中与 JD 要求相关、但表述不够贴合的条目。
硬性规则（违反任何一条都会被程序拦截）：
1. 只能换说法、调整顺序和侧重点，不得新增原文没有的事实：数字、技术名、公司、项目、职责范围都不能加；
2. 不得拔高角色或程度：原文是"参与"就不能写成"主导"，"了解"不能写成"精通"；不得去掉限定词改变范围（"参与前端开发"不能写成"参与 AI 应用开发"）；
3. 每条改写只基于一条原文（可以是跨行的同一条），lines 填原文行号，original 照抄原文；
4. 只改写最能提升匹配度的 {max_items} 条以内，不需要改的不要改；
5. [NAME_1] 等是脱敏占位符，原样保留；
6. 不得在句子里追加原文没有的能力、领域、技术描述（如"并能审查代码""熟悉某业务""微服务""多智能体"）；
7. 不得弱化原文：精通、熟练、熟悉等程度词照原文写，不要改低。

岗位：{title}
JD 要求及当前匹配情况：
{gaps}

简历（每行开头是行号）：
{resume}"""

REVIEW_PROMPT = """你是严格的简历事实核查员。下面每一项是"原文"和"改写"，判断改写有没有夸大或编造。
原文是简历中的几行上下文，改写只对应其中一条。
逐个短语核对：改写里每一个表示能力、技术、领域、范围、成果的词或短语，都必须能在原文中找到相同或同义的说法。
- 通过：换说法、调整语序、删减内容、补上原文已有的同义表达、只加一个分类小标题（如"（Agent 方向）"）；
- 不通过：数字变化；新增技术或成果；角色升级（参与→主导）；范围扩大（某模块→整个系统，前端调用→构建某能力）；
  程度加深（了解→精通）；句末或句中追加原文没有的能力或领域描述（如"并能审查代码""熟悉某业务字段""微服务""多智能体""原理"）。
宁严勿松：拿不准时判夸大，用户会再确认一次。

{items}"""


def source_text(lines: list[str], ids: list[int]) -> str:
    """依据原文：引用行首尾之间再各扩 1 行（PDF 断行容错，与 check_quotes 一致）。"""
    if not ids or any(not 1 <= n <= len(lines) for n in ids):
        return ""
    return "".join(lines[max(min(ids) - 2, 0):max(ids) + 1])


def rule_check(new_text: str, source: str, full: str = "") -> list[str]:
    """第一层：确定性检查。返回问题列表。full 是整份简历：原文这条没有、但简历别处有的，提示出来方便用户判断。"""
    if not source:
        return ["依据行号无效"]
    problems = []
    new_text = PLACEHOLDER.sub("", new_text)  # [NAME_1] 等占位符里的字母数字不算
    src_compact = "".join(source.split())
    src_numbers = set(NUMBER.findall(src_compact))
    for n in NUMBER.findall(new_text):
        if n not in src_numbers:
            problems.append(f"数字 {n} 在原文中没有" + _elsewhere(n, full))
    src_lower = src_compact.lower()
    for t in sorted(set(TECH.findall(new_text))):
        if t.lower() not in src_lower and not any(z in src_compact for z in SYNONYMS.get(t.lower(), [])):
            problems.append(f"技术名 {t} 在原文中没有" + _elsewhere(t, full))
    for w in ESCALATION_WORDS:
        if w in new_text and w not in source:
            problems.append(f"疑似拔高：原文没有'{w}'")
    for w in CN_TERMS:
        if w in new_text and w not in source:
            problems.append(f"新增技术/能力词：原文没有'{w}'")
    new_c = "".join(new_text.split())
    if "开发" in new_c or "研发" in new_c:
        for q in QUALIFIERS:
            if (q + "开发" in src_compact or q + "研发" in src_compact) and q + "开发" not in new_c and q + "研发" not in new_c:
                problems.append(f"去掉了限定词'{q}'：原文是{q}开发，改写可能扩大了职责范围")
    for ph in new_phrases(new_text, source):
        problems.append(f"新增表述：'{ph}' 原文没有，请确认属实")
    return problems


def new_phrases(new_text: str, source: str, min_len: int = NEW_PHRASE_MIN) -> list[str]:
    """改写里"没有依据"的中文片段：某个字和它左右相邻的字组成的二字词，在原文任何位置都没出现过，这个字就算没依据；
    连续 ≥ min_len 个没依据的中文字算一段新增表述。

    为什么按二字词而不是按顺序逐字比对：实测改写常调整语序，逐字比对会把整段重排都当成新增（28 条正常改写误报 20 条）。
    为什么只看中文：英文技术名、数字已经有专门的检查，避免同一处报两次。
    模型复核对"一句真话后面加一小段"不敏感（第 4 周真实改写评测：放过 12%），这一层是确定性兜底。
    """
    a = "".join(PLACEHOLDER.sub("", new_text).split())
    b = "".join(source.split())
    grams = {b[i:i + 2] for i in range(len(b) - 1)}
    out, run = [], ""
    for i, ch in enumerate(a):
        covered = any(g in grams for g in (a[i - 1:i + 1] if i else "", a[i:i + 2]) if len(g) == 2)
        if "一" <= ch <= "龥" and not covered:
            run += ch
            continue
        if len(run) >= min_len:
            out.append(run)
        run = ""
    if len(run) >= min_len:
        out.append(run)
    return out


def _elsewhere(token: str, full: str) -> str:
    return "（简历其他位置有，属于跨条目拼接）" if full and token.lower() in "".join(full.split()).lower() else ""


def rewrite_resume(gap: GapReport, title: str, lines: list[str], role: Role = "strong",
                   max_items: int = 6) -> list[RewriteItem]:
    gaps = "\n".join(f"R{m.req_id}（{m.status}）: {gap.requirements[m.req_id - 1]}" for m in gap.matches)
    prompt = REWRITE_PROMPT.format(max_items=max_items, title=title, gaps=gaps,
                                   resume="\n".join(f"L{i}: {line}" for i, line in enumerate(lines, 1)))
    items = structured(Rewrite, role).invoke(prompt).items
    norm = lambda x: "".join(x.split())
    out, seen = [], set()
    for it in items:
        # 去掉：原样返回的（实测偶尔出现）；同一条原文的第二个版本（实测会给同一条出两种改法，用户只能用一种）
        if norm(it.new_text) != norm(it.original) and norm(it.original) not in seen:
            seen.add(norm(it.original))
            out.append(it)
    return out


def model_review(pairs: list[tuple[str, str]], role: Role = "strong") -> list[Verdict]:
    """第二层：模型复核。pairs = [(原文, 改写)]，返回与 pairs 一一对应的判断（缺的按'编造'处理，宁严勿松）。"""
    if not pairs:
        return []
    items = "\n\n".join(f"#{i}\n原文：{src}\n改写：{new}" for i, (src, new) in enumerate(pairs))
    got = {v.index: v for v in structured(Review, role).invoke(REVIEW_PROMPT.format(items=items)).verdicts}
    return [got.get(i, Verdict(index=i, verdict="编造", reason="复核漏判，按不通过处理")) for i in range(len(pairs))]


def check_items(items: list[RewriteItem], lines: list[str], role: Role = "strong") -> list[dict]:
    """两层校验，返回每条改写的检查结果：{item, source, rule_problems, verdict, reason, flagged}。"""
    sources = [source_text(lines, it.lines) for it in items]
    verdicts = model_review([(s, it.new_text) for s, it in zip(sources, items)], role)
    results = []
    for it, src, v in zip(items, sources, verdicts):
        rp = rule_check(it.new_text, src, "".join(lines))
        results.append({"item": it.model_dump(), "source": src, "rule_problems": rp,
                        "verdict": v.verdict, "reason": v.reason, "flagged": bool(rp) or v.verdict != "通过"})
    return results
