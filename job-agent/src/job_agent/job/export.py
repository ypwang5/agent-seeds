"""把采纳的改写套回简历全文，导出 Word。

定位方式和防编造校验一致：按改写记录的行号（前后各容错 1 行，PDF 会在句子中间断行）找到原文，
在这几行拼起来的文字里精确查找 original，找到才替换；找不到的不猜，列在文末让用户自己改。
改过的段落标黄，投递前用户核对后可以取消高亮。
"""
import io

from docx import Document
from docx.enum.text import WD_COLOR_INDEX
from docx.shared import Pt

from job_agent.job.parse import number_lines


def apply_rewrites(resume_text: str, rewrites: list[dict]) -> tuple[list[tuple[str, bool]], list[dict]]:
    """返回（[(段落文字, 是否改过)], 没能定位的改写）。rewrites：[{lines, original, final_text}]。"""
    lines: list[str | None] = list(number_lines(resume_text))
    changed: set[int] = set()
    missed = []
    for r in rewrites:
        if not r.get("final_text") or r["final_text"] == r["original"]:
            continue
        ids = [n for n in r.get("lines", []) if 1 <= n <= len(lines)]
        done = False
        if ids:
            lo, hi = max(min(ids) - 2, 0), min(max(ids) + 1, len(lines))  # 下标范围 [lo, hi)
            # 从最短的连续几行开始试，找到包含原文的那一段就替换（避免把无关的相邻行并进来）
            spans = sorted(((a, b) for a in range(lo, hi) for b in range(a + 1, hi + 1)), key=lambda x: x[1] - x[0])
            for a, b in spans:
                if any(lines[i] is None for i in range(a, b)):
                    continue  # 已经被别的改写合并过
                for sep in ("", " "):
                    joined = sep.join(lines[a:b])
                    if r["original"] in joined:
                        lines[a] = joined.replace(r["original"], r["final_text"], 1)
                        for i in range(a + 1, b):
                            lines[i] = None
                        changed.add(a)
                        done = True
                        break
                if done:
                    break
        if not done:
            missed.append(r)
    return [(t, i in changed) for i, t in enumerate(lines) if t is not None], missed


def to_docx(paragraphs: list[tuple[str, bool]], missed: list[dict]) -> bytes:
    doc = Document()
    style = doc.styles["Normal"]
    style.font.size = Pt(10.5)
    style.font.name = "Microsoft YaHei"
    note = doc.add_paragraph()
    note.add_run("说明：黄色高亮为本次改写的内容，请核对后再投递（全选 → 取消突出显示即可去掉黄色）。"
                 "版式是从 PDF 提取的文字，排版请以你的原简历为准。").italic = True
    for text, changed in paragraphs:
        run = doc.add_paragraph().add_run(text)
        if changed:
            run.font.highlight_color = WD_COLOR_INDEX.YELLOW
    if missed:
        doc.add_heading("没能自动替换的改写（请手动修改）", level=2)
        for r in missed:
            doc.add_paragraph(f"原文：{r['original']}")
            doc.add_paragraph(f"改为：{r['final_text']}")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
