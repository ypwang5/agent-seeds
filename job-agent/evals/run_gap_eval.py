"""差距分析评测：每份简历 × 每个有标注的 JD 做一次差距分析，输出供人工抽查的明细。

运行：uv run python evals/run_gap_eval.py [--model deepseek:deepseek-v4-pro]
- JD 优先读解析评测的缓存（evals/out/<快模型>/同名.json），没有再现场解析
- 明细写到 evals/out/gap/<模型>/简历名__JD名.md，汇总表追加到 evals/out/gap_report.md
"""
import argparse
import time
from datetime import datetime
from pathlib import Path

from langchain_core.callbacks import get_usage_metadata_callback

from job_agent.core.llm import model_spec
from job_agent.core.pii import Masker
from job_agent.job.gap import analyze_gap
from job_agent.job.parse import number_lines, parse_jd, read_text
from job_agent.job.schemas import JobPosting

ROOT = Path(__file__).parent


def safe(spec: str) -> str:
    return spec.replace(":", "_").replace("/", "_")


def load_jd(stem: str) -> JobPosting:
    cached = ROOT / "out" / safe(model_spec("fast")) / f"{stem}.json"
    if cached.exists():
        return JobPosting.model_validate_json(cached.read_text(encoding="utf-8"))
    return parse_jd((ROOT / "jds" / f"{stem}.txt").read_text(encoding="utf-8"))


def detail(jd: JobPosting, report, lines: list[str]) -> str:
    out = [f"# {jd.title}（{jd.company or '公司未公开'}）\n",
           f"匹配分 **{report.score:.0%}**：命中 {report.count('命中')} / 部分 {report.count('部分')} / "
           f"缺失 {report.count('缺失')}，共 {len(report.requirements)} 条\n",
           "| # | 要求 | 判断 | 依据（简历原文） | 理由 |", "|---|---|---|---|---|"]
    for m in report.matches:
        evidence = "<br>".join(f"L{','.join(map(str, b.lines))}: {b.text}" for b in m.evidence)
        out.append(f"| R{m.req_id} | {report.requirements[m.req_id - 1]} | {m.status} | {evidence} | {m.reason} |")
    if report.problems:
        out += ["\n## 校验问题", *[f"- {p}" for p in report.problems]]
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", help="如 deepseek:deepseek-v4-pro；默认用 JOB_STRONG_MODEL")
    args = ap.parse_args()
    if args.model:
        import os
        os.environ["JOB_STRONG_MODEL"] = args.model
    spec = model_spec("strong")
    out_dir = ROOT / "out" / "gap" / safe(spec)
    out_dir.mkdir(parents=True, exist_ok=True)

    stems = sorted(p.stem for p in (ROOT / "labels").glob("*.json"))
    rows = []
    for resume_path in sorted((ROOT / "resumes").glob("*")):
        if resume_path.suffix.lower() not in {".pdf", ".txt", ".md"}:
            continue
        lines = number_lines(Masker().mask(read_text(resume_path)))  # 只脱敏，不需要先解析成结构
        for i, stem in enumerate(stems):
            jd = load_jd(stem)
            t = time.time()
            with get_usage_metadata_callback() as cb:
                report = analyze_gap(jd, lines)
            sec = time.time() - t
            tokens = sum(u["total_tokens"] for u in cb.usage_metadata.values())
            (out_dir / f"{resume_path.stem}__{stem}.md").write_text(detail(jd, report, lines), encoding="utf-8")
            rows.append(f"| {stem} | {report.score:.0%} | {report.count('命中')}/{report.count('部分')}/"
                        f"{report.count('缺失')} | {len(report.problems)} | {sec:.1f}s | {tokens} |")
            print(f"[{i + 1}/{len(stems)}] {stem} 完成")

    text = "\n".join([f"\n## {datetime.now():%Y-%m-%d %H:%M} · {spec}\n",
                      "| JD | 匹配分 | 命中/部分/缺失 | 校验问题 | 耗时 | tokens |", "|---|---|---|---|---|---|",
                      *rows, f"\n明细：{out_dir.relative_to(ROOT)}\n"])
    with open(ROOT / "out" / "gap_report.md", "a", encoding="utf-8") as f:
        f.write(text + "\n")
    print("汇总已追加到 evals/out/gap_report.md")


if __name__ == "__main__":
    main()
