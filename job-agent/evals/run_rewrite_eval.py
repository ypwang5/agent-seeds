"""改写编造率评测：多份简历 × 多个 JD 跑"差距分析 → 改写 → 两层校验"，导出所有自动通过的改写供人工逐条判断。

运行：uv run python evals/run_rewrite_eval.py [--jds 02 07 11]
- 简历：evals/resumes/（真实，不提交）+ evals/mock_resumes/（虚构，背景不同，避免结论只对一个人成立）
- 编造率 = 两层都放过、但人工判断为编造的条数 / 改写总数。程序算不出"人工判断"，所以输出明细表，
  人工在"人工判断"列填 对/编造，再把结论记进笔记
- 明细写到 evals/out/rewrite_eval.md（含简历片段，不提交）
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from job_agent.core.pii import Masker
from job_agent.job.gap import analyze_gap
from job_agent.job.parse import number_lines, parse_jd, read_text
from job_agent.job.rewrite import check_items, rewrite_resume

ROOT = Path(__file__).parent


def resumes() -> list[Path]:
    return [p for d in ("resumes", "mock_resumes") for p in sorted((ROOT / d).glob("*"))
            if p.suffix.lower() in {".pdf", ".txt", ".md"}]


def run_pair(resume: Path, jd_path: Path) -> dict:
    lines = number_lines(Masker().mask(read_text(resume)))
    jd = parse_jd(jd_path.read_text(encoding="utf-8"))
    gap = analyze_gap(jd, lines)
    results = check_items(rewrite_resume(gap, jd.title, lines), lines)
    return {"resume": resume.stem, "jd": jd_path.stem, "score": gap.score, "results": results}


def cell(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jds", nargs="+", default=["02", "07", "11"], help="JD 编号前缀")
    args = ap.parse_args()
    jds = [p for p in sorted((ROOT / "jds").glob("*.txt")) if p.name[:2] in args.jds]
    pairs = [(r, j) for r in resumes() for j in jds]
    with ThreadPoolExecutor(3) as pool:
        runs = list(pool.map(lambda x: run_pair(*x), pairs))

    out = [f"\n## {datetime.now():%Y-%m-%d %H:%M} · {len(resumes())} 份简历 × {len(jds)} 个 JD\n",
           "| 简历 | JD | 匹配分 | 改写 | 被拦（交人工） | 自动通过 |", "|---|---|---|---|---|---|"]
    passed = []
    for r in runs:
        flagged = sum(x["flagged"] for x in r["results"])
        out.append(f"| {r['resume']} | {r['jd']} | {r['score']:.0%} | {len(r['results'])} | {flagged} "
                   f"| {len(r['results']) - flagged} |")
        passed += [(r["resume"], x) for x in r["results"] if not x["flagged"]]
    total = sum(len(r["results"]) for r in runs)
    out += [f"\n合计改写 {total} 条，被拦 {total - len(passed)} 条，自动通过 {len(passed)} 条。\n",
            "### 自动通过的改写（人工逐条判断）\n", "| # | 简历 | 依据原文 | 改写 | 人工判断 |", "|---|---|---|---|---|"]
    out += [f"| {i} | {name} | {cell(x['source'])} | {cell(x['item']['new_text'])} | |"
            for i, (name, x) in enumerate(passed, 1)]
    text = "\n".join(out)
    print(text)
    with open(ROOT / "out" / "rewrite_eval.md", "a", encoding="utf-8") as f:
        f.write(text + "\n")


if __name__ == "__main__":
    main()
