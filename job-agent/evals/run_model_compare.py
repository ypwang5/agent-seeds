"""多模型对比：同一批任务换不同模型跑，比质量、成功率、成本、耗时，据此决定"哪一步用哪个模型"。

运行：uv run python evals/run_model_compare.py [--models deepseek:deepseek-flash qwen:qwen-plus ...]

任务（都是线上真实会调用的步骤）：
1. 筛选链路 × 7 个有标注的 JD：解析 JD → 差距分析 → JD 风险
   - 解析：硬性要求召回、关键词 F1（对比 labels/）
   - 差距分析：引用核对出问题的条数；匹配分与参照模型（v4-pro）的平均差（没有人工标注的匹配分，只能比一致性）
   - 风险：风险类型的精确率 / 召回率（对比 risk_labels.json）
2. 防编造复核 × 24 条人工样本：漏检（编造没拦住）、误报（诚实改写被拦）
成本 = tokens × 单价（PRICES，2026-10-28 查官网 / OpenRouter；美元按 7.1 换算；DeepSeek 按高峰价，偏保守）。
结果追加到 evals/out/model_compare.md，明细在 evals/out/model_compare/<模型>.json。
"""
import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from langchain_core.callbacks import get_usage_metadata_callback

from job_agent.core.pii import Masker
from job_agent.job.gap import analyze_gap
from job_agent.job.parse import number_lines, parse_jd, read_text
from job_agent.job.rewrite import model_review, rule_check
from job_agent.job.risk import jd_risks
from run_parse_eval import keyword_pr, recall

ROOT = Path(__file__).parent
USD = 7.1
# 元 / 百万 tokens：（输入, 输出）
PRICES = {
    "deepseek-flash": (2, 8), "deepseek-v4-pro": (9, 27),
    "qwen-plus": (0.8, 2), "qwen3.7-plus": (2, 8),
    "anthropic/claude-haiku-5.5": (0.1 * USD, 0.5 * USD), "anthropic/claude-sonnet-5.5": (2 * USD, 10 * USD),
}
DEFAULT_MODELS = ["deepseek:deepseek-flash", "deepseek:deepseek-v4-pro", "qwen:qwen-plus", "qwen:qwen3.7-plus",
                  "openrouter:anthropic/claude-haiku-5.5", "openrouter:anthropic/claude-sonnet-5.5"]
REFERENCE = "deepseek:deepseek-v4-pro"


def cost(usage: dict, spec: str) -> float:
    """usage：get_usage_metadata_callback 收集的 {模型名: 用量}。"""
    p_in, p_out = PRICES[spec.split(":", 1)[1]]
    return sum(u["input_tokens"] * p_in + u["output_tokens"] * p_out for u in usage.values()) / 1e6


def screen_one(stem: str, lines: list[str]) -> dict:
    text = (ROOT / "jds" / f"{stem}.txt").read_text(encoding="utf-8")
    label = json.loads((ROOT / "labels" / f"{stem}.json").read_text(encoding="utf-8"))
    out = {"id": stem}
    t = time.time()
    with get_usage_metadata_callback() as cb:
        try:
            jd = parse_jd(text)
            kp, kr = keyword_pr(label["tech_keywords"], jd.tech_keywords)
            out.update(req_recall=recall(label["hard_requirements"], jd.hard_requirements),
                       kw_f1=2 * kp * kr / (kp + kr) if kp + kr else 0.0)
            gap = analyze_gap(jd, lines, role="fast")
            out.update(score=gap.score, gap_problems=len(gap.problems))
            risks, _ = jd_risks(text, jd.company)
            out["risk_types"] = sorted({r.type for r in risks})
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    out.update(seconds=time.time() - t, usage=dict(cb.usage_metadata))
    return out


def run_model(spec: str, stems: list[str], lines: list[str]) -> dict:
    os.environ["JOB_FAST_MODEL"] = os.environ["JOB_STRONG_MODEL"] = spec
    with ThreadPoolExecutor(4) as pool:
        jobs = list(pool.map(lambda s: screen_one(s, lines), stems))
    cases = json.loads((ROOT / "fabrication_cases.json").read_text(encoding="utf-8"))
    t = time.time()
    with get_usage_metadata_callback() as cb:
        try:
            verdicts = [v.verdict for v in model_review([(c["src"], c["new"]) for c in cases])]
            review_error = None
        except Exception as e:
            verdicts, review_error = [], f"{type(e).__name__}: {str(e)[:120]}"
    review = {"seconds": time.time() - t, "usage": dict(cb.usage_metadata), "error": review_error}
    if verdicts:
        model_caught = [v != "通过" for v in verdicts]
        both = [m or bool(rule_check(c["new"], c["src"])) for m, c in zip(model_caught, cases)]
        fab = [c["fabricated"] for c in cases]
        review.update(
            miss_model=sum(f and not m for f, m in zip(fab, model_caught)),
            false_alarm=sum(m and not f for f, m in zip(fab, model_caught)),
            miss_both=sum(f and not b for f, b in zip(fab, both)), n_fab=sum(fab), n_ok=len(fab) - sum(fab))
    return {"model": spec, "jobs": jobs, "review": review}


def summarize(res: dict, ref: dict | None, labels: dict) -> str:
    spec, ok = res["model"], [j for j in res["jobs"] if "error" not in j]
    n = len(res["jobs"])
    avg = lambda k: sum(j[k] for j in ok) / len(ok) if ok else 0.0
    tp = fp = fn = 0
    for j in ok:
        got, exp = set(j["risk_types"]), labels[j["id"]]
        tp, fp, fn = tp + len(got & exp), fp + len(got - exp), fn + len(exp - got)
    ref_scores = {j["id"]: j["score"] for j in (ref or {}).get("jobs", []) if "score" in j}
    diffs = [abs(j["score"] - ref_scores[j["id"]]) for j in ok if j["id"] in ref_scores]
    screen_cost = sum(cost(j["usage"], spec) for j in res["jobs"]) / n
    r = res["review"]
    review_cell = (f"{r['miss_model']}/{r['n_fab']} · {r['false_alarm']}/{r['n_ok']} · {r['miss_both']}"
                   if "miss_model" in r else f"失败 {r['error']}")
    return (f"| {spec} | {len(ok)}/{n} | {avg('req_recall'):.0%} | {avg('kw_f1'):.0%} | {avg('gap_problems'):.1f} "
            f"| {sum(diffs) / len(diffs) * 100 if diffs else 0:.0f} | {tp / max(tp + fp, 1):.0%} / {tp / max(tp + fn, 1):.0%} "
            f"| {avg('seconds'):.1f}s | {screen_cost * 100:.2f} 分 | {review_cell} | {r['seconds']:.0f}s "
            f"| {cost(r['usage'], spec) * 100:.2f} 分 |")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", default=DEFAULT_MODELS)
    args = ap.parse_args()
    labels = {k: set(v) for k, v in json.loads((ROOT / "risk_labels.json").read_text(encoding="utf-8")).items()
              if not k.startswith("_")}
    stems = sorted(labels)
    lines = number_lines(Masker().mask(read_text(next((ROOT / "resumes").glob("*.pdf")))))   # 真实简历不进仓库
    out_dir = ROOT / "out" / "model_compare"
    out_dir.mkdir(parents=True, exist_ok=True)

    results = {}
    for spec in args.models:
        print(f"跑 {spec} ……", flush=True)
        results[spec] = run_model(spec, stems, lines)
        (out_dir / f"{spec.replace(':', '_').replace('/', '_')}.json").write_text(
            json.dumps(results[spec], ensure_ascii=False, indent=1), encoding="utf-8")
    ref = results.get(REFERENCE) or (json.loads(p.read_text(encoding="utf-8")) if (
        p := out_dir / f"{REFERENCE.replace(':', '_')}.json").exists() else None)

    rows = [summarize(results[s], ref, labels) for s in args.models]
    text = "\n".join([
        f"\n## {datetime.now():%Y-%m-%d %H:%M} · {len(stems)} 个 JD + 24 条防编造样本\n",
        "| 模型 | 筛选成功 | 硬性要求召回 | 关键词 F1 | 差距引用问题/JD | 与 v4-pro 分差 | 风险 准确/召回 "
        "| 筛选耗时/JD | 筛选成本/JD | 复核 漏检·误报·两层合计漏检 | 复核耗时 | 复核成本 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|", *rows,
        "\n分差单位是百分点；成本单位是人民币分（1 元 = 100 分）。复核一栏：模型单独漏检 / 编造数 · 误报 / 诚实数 · 加上程序检查后仍漏检数。"])
    print(text)
    with open(ROOT / "out" / "model_compare.md", "a", encoding="utf-8") as f:
        f.write(text + "\n")


if __name__ == "__main__":
    main()
