"""解析评测：跑 evals/jds 下所有 JD 和 evals/resumes 下所有简历，输出指标表。

运行：uv run python evals/run_parse_eval.py [--model deepseek:deepseek-flash]
- 有标注（evals/labels/同名.json）的 JD：计算硬性要求召回率、技术关键词准确率/召回率、用工线索召回率
- 简历：统计行号引用核对的问题率（第 2 周防编造的基础）
- 结果保存在 evals/out/<模型>/，汇总表追加到 evals/out/report.md
"""
import argparse
import json
import time
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from langchain_core.callbacks import get_usage_metadata_callback

from job_agent.core.llm import model_spec
from job_agent.job.parse import check_citations, parse_jd, parse_resume, read_text

ROOT = Path(__file__).parent


def norm(s: str) -> str:
    return "".join(s.lower().split())


def recall(gold: list[str], pred: list[str], threshold: float = 0.5) -> float:
    """每条标准答案在预测里找最相似的一条，相似度 ≥ 阈值算命中（要求是自然语言，没法精确匹配）。"""
    if not gold:
        return 1.0
    def match(a: str, b: str) -> bool:  # 双向包含都算命中，如"驻场"与"项目驻场某银行"
        a, b = norm(a), norm(b)
        return a in b or b in a or SequenceMatcher(None, a, b).ratio() >= threshold
    hit = sum(any(match(g, p) for p in pred) for g in gold)
    return hit / len(gold)


def keyword_pr(gold: list[str], pred: list[str]) -> tuple[float, float]:
    """双向包含算命中，如"内核驱动"与"内核驱动开发"。短词（如 Go）只认完全相同，免得命中 MongoDB。"""
    g, p = {norm(x) for x in gold}, {norm(x) for x in pred}
    def match(a: str, b: str) -> bool:
        short, long = sorted((a, b), key=len)
        return a == b or (len(short) >= 3 and short in long)
    precision = sum(any(match(x, y) for y in g) for x in p) / len(p) if p else 0.0
    recall_ = sum(any(match(x, y) for y in p) for x in g) / len(g) if g else 1.0
    return precision, recall_


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", help="如 deepseek:deepseek-flash；默认用 JOB_FAST_MODEL")
    args = ap.parse_args()
    spec = args.model or model_spec("fast")
    if args.model:  # 让 parse_jd / parse_resume 用指定模型
        import os
        os.environ["JOB_FAST_MODEL"] = args.model
    out_dir = ROOT / "out" / spec.replace(":", "_").replace("/", "_")
    out_dir.mkdir(parents=True, exist_ok=True)

    rows, totals = [], {"tokens": 0, "seconds": 0.0, "fail": 0}
    for jd_path in sorted((ROOT / "jds").glob("*.txt")):
        t = time.time()
        try:
            with get_usage_metadata_callback() as cb:
                jd = parse_jd(jd_path.read_text(encoding="utf-8"))
        except Exception as e:
            totals["fail"] += 1
            rows.append(f"| {jd_path.stem} | 失败：{type(e).__name__} | | | | | |")
            continue
        sec = time.time() - t
        tokens = sum(u["total_tokens"] for u in cb.usage_metadata.values())
        totals["tokens"] += tokens
        totals["seconds"] += sec
        (out_dir / f"{jd_path.stem}.json").write_text(jd.model_dump_json(indent=2), encoding="utf-8")
        label_path = ROOT / "labels" / f"{jd_path.stem}.json"
        if label_path.exists():
            label = json.loads(label_path.read_text(encoding="utf-8"))
            req = recall(label["hard_requirements"], jd.hard_requirements)
            kp, kr = keyword_pr(label["tech_keywords"], jd.tech_keywords)
            gold_sig = label.get("employment_signals", [])
            sig_r = recall(gold_sig, jd.employment_signals)
            sig_p = recall(jd.employment_signals, gold_sig)  # 反过来：预测的每条是否在标准答案里 = 准确率
            edu_ok = "本科" not in label.get("education", "") or "本科" in (jd.education or "")
            years_ok = label.get("years_required") == jd.years_required
            mark = lambda ok: "对" if ok else "错"  # 不用 ✓：Windows 控制台默认 GBK 编码打印不了
            rows.append(f"| {jd_path.stem} | {mark(edu_ok)} / {mark(years_ok)} | {req:.0%} | {kp:.0%} / {kr:.0%} "
                        f"| {sig_p:.0%} / {sig_r:.0%} | {sec:.1f}s | {tokens} |")
        else:
            rows.append(f"| {jd_path.stem} | | 未标注 | | | {sec:.1f}s | {tokens} |")

    resume_rows = []
    for path in sorted((ROOT / "resumes").glob("*")):
        if path.suffix.lower() not in {".pdf", ".txt", ".md"}:
            continue
        pr = parse_resume(read_text(path))
        bullets = pr.resume.all_bullets()
        problems = check_citations(bullets, pr.lines)
        (out_dir / f"resume_{path.stem}.json").write_text(pr.resume.model_dump_json(indent=2), encoding="utf-8")
        resume_rows.append(f"| 简历 {len(resume_rows) + 1} | {len(bullets)} | {len(problems)} |")

    n = len(rows)
    report = [f"\n## {datetime.now():%Y-%m-%d %H:%M} · {spec}\n",
              "| JD | 学历/年限 | 硬性要求召回 | 关键词 准确/召回 | 用工线索 准确/召回 | 耗时 | tokens |",
              "|---|---|---|---|---|---|---|",
              *rows,
              f"\nJD 共 {n} 个，失败 {totals['fail']} 个，平均耗时 {totals['seconds'] / max(n - totals['fail'], 1):.1f}s，"
              f"总 tokens {totals['tokens']}\n"]
    if resume_rows:
        report += ["| 简历 | bullet 数 | 引用问题数 |", "|---|---|---|", *resume_rows]
    text = "\n".join(report)
    print(text)
    with open(ROOT / "out" / "report.md", "a", encoding="utf-8") as f:
        f.write(text + "\n")


if __name__ == "__main__":
    main()
