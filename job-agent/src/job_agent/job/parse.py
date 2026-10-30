"""JD 与简历解析。

关键设计：简历先由程序编上行号（L1、L2……），模型抽取的每一条都要标出来自哪几行。
这样第 2 周做"防编造"时，程序能直接核对改写内容是否有原文依据，不用相信模型的自觉。
"""
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from pypdf import PdfReader

from job_agent.core.llm import Role, structured
from job_agent.core.pii import Masker
from job_agent.job.schemas import Bullet, JobPosting, Resume

JD_PROMPT = """你是招聘信息分析助手。从下面的 JD 中抽取结构化信息。
规则：只抽取原文里有的内容，不要推测；没有的字段留空。

JD 原文：
{jd}"""

RESUME_PROMPT = """你是简历解析助手。下面的简历每行开头有行号（如 L12:）。
规则：
1. 每一条 bullet 的 text 照抄原文（可去掉行首的符号），不改写、不合并不同的成果；
2. lines 填这一条所在的行号；跨行就填多个；
3. 只抽取原文有的内容。

简历：
{resume}"""


def read_text(path: str | Path) -> str:
    path = Path(path)
    if path.suffix.lower() == ".pdf":
        return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    return path.read_text(encoding="utf-8")


def number_lines(text: str) -> list[str]:
    """去掉空行，返回行列表；行号 = 下标 + 1。"""
    return [line.strip() for line in text.splitlines() if line.strip()]


def parse_jd(jd_text: str, role: Role = "fast") -> JobPosting:
    jd: JobPosting = structured(JobPosting, role).invoke(JD_PROMPT.format(jd=jd_text))
    # 确定性过滤：用工线索必须是原文片段（实测模型会把字段说明里的示例词当答案编出来）
    source = "".join(jd_text.split())
    jd.employment_signals = [s for s in jd.employment_signals if "".join(s.split()) in source]
    return jd


@dataclass
class ParsedResume:
    resume: Resume      # 结构化结果（个人信息仍是占位符，展示给用户时用 masker.unmask 还原）
    lines: list[str]    # 脱敏后的原文行，行号从 1 开始
    masker: Masker      # 保留映射，后续步骤输出时还原


def parse_resume(resume_text: str, names: list[str] | None = None, role: Role = "fast") -> ParsedResume:
    masker = Masker(names)
    lines = number_lines(masker.mask(resume_text))
    numbered = "\n".join(f"L{i}: {line}" for i, line in enumerate(lines, 1))
    resume: Resume = structured(Resume, role).invoke(RESUME_PROMPT.format(resume=numbered))
    return ParsedResume(resume, lines, masker)


def quote_coverage(quote: str, source: str, min_block: int = 4) -> float:
    """摘录有多大比例能在原文里找到：按省略号切成片段，统计和原文连续相同（≥ min_block 个字）的部分。

    只数长度 ≥ min_block 的相同片段，避免"的""和"这类单字到处都能匹配上而虚高。
    """
    source = "".join(source.split())
    total = found = 0
    for frag in re.split(r"…+|\.{3,}", quote):
        frag = "".join(frag.split())
        if not frag:
            continue
        total += len(frag)
        if frag in source:
            found += len(frag)
        else:
            blocks = SequenceMatcher(None, frag, source, autojunk=False).get_matching_blocks()
            found += sum(b.size for b in blocks if b.size >= min_block)
    return found / total if total else 0.0


def check_quotes(quotes: list[Bullet], lines: list[str], min_coverage: float = 0.8) -> list[str]:
    """核对"摘录"：行号存在，且摘录的内容基本都能在这些行里找到。用于差距分析等引用简历片段的场景。

    原文范围取引用行的首尾之间，再各向外扩 1 行：PDF 会在句子中间断行，实测模型常把跨行的句子只标成一行。
    """
    problems = []
    for q in quotes:
        if not q.lines or any(not 1 <= n <= len(lines) for n in q.lines):
            problems.append(f"行号无效 {q.lines}：{q.text[:30]}")
            continue
        cov = quote_coverage(q.text, "".join(lines[max(min(q.lines) - 2, 0):max(q.lines) + 1]))
        if cov < min_coverage:
            problems.append(f"与原文不符（覆盖率 {cov:.0%}）L{q.lines}：{q.text[:30]}")
    return problems


def check_citations(bullets: list[Bullet], lines: list[str], min_similarity: float = 0.6) -> list[str]:
    """确定性检查：每条 bullet 引用的行号是否存在、内容是否和原文对得上。返回问题列表。"""
    problems = []
    for b in bullets:
        if not b.lines or any(not 1 <= n <= len(lines) for n in b.lines):
            problems.append(f"行号无效 {b.lines}：{b.text[:30]}")
            continue
        source = "".join(lines[n - 1] for n in sorted(b.lines))
        if b.text not in source:
            # 不是原样子串时，用相似度兜底（PDF 换行、去掉行首符号等会造成细微差异）
            ratio = SequenceMatcher(None, b.text, source).ratio()
            if ratio < min_similarity:
                problems.append(f"与原文不符（相似度 {ratio:.2f}）L{b.lines}：{b.text[:30]}")
    return problems
