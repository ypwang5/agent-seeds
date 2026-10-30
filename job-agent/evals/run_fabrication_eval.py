"""防编造评测：在人工构造的"原文 → 改写"样本上，看两层校验各抓住多少编造、误报多少诚实改写。

运行：uv run python evals/run_fabrication_eval.py [--model deepseek:deepseek-v4-pro]
- 样本在 evals/fabrication_cases.json（不含真实个人信息）
- 漏检率 = 编造样本里两层都没拦住的比例，这就是"编造率"的上界（第 2 周目标 < 2%）
"""
import argparse
import json
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from job_agent.core.llm import model_spec
from job_agent.job.rewrite import model_review, rule_check

ROOT = Path(__file__).parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", help="复核用的模型，默认 JOB_STRONG_MODEL")
    args = ap.parse_args()
    if args.model:
        os.environ["JOB_STRONG_MODEL"] = args.model
    cases = json.loads((ROOT / "fabrication_cases.json").read_text(encoding="utf-8"))
    verdicts = model_review([(c["src"], c["new"]) for c in cases])

    by_type = defaultdict(lambda: [0, 0, 0, 0])  # 样本数, 程序抓到, 模型抓到, 合计抓到
    rows = []
    for c, v in zip(cases, verdicts):
        rule = bool(rule_check(c["new"], c["src"]))
        model = v.verdict != "通过"
        t = by_type[(c["fabricated"], c["type"])]
        t[0] += 1
        t[1] += rule
        t[2] += model
        t[3] += rule or model
        if (rule or model) != c["fabricated"]:
            rows.append(f"- {'漏检' if c['fabricated'] else '误报'}【{c['type']}】{c['new']}（复核：{v.verdict}，{v.reason}）")

    def total(fab: bool, k: int) -> tuple[int, int]:
        items = [x for (f, _), x in by_type.items() if f == fab]
        return sum(x[k] for x in items), sum(x[0] for x in items)

    lines = [f"\n## {datetime.now():%Y-%m-%d %H:%M} · 复核模型 {model_spec('strong')}\n",
             "| 样本 | 类型 | 数量 | 程序检查 | 模型复核 | 合计 |", "|---|---|---|---|---|---|"]
    for (fab, typ), (n, r, m, a) in sorted(by_type.items()):
        lines.append(f"| {'编造' if fab else '诚实'} | {typ} | {n} | {r} | {m} | {a} |")
    caught, n_fab = total(True, 3)
    false_alarm, n_ok = total(False, 3)
    lines += [f"\n编造样本 {n_fab} 条：程序抓到 {total(True, 1)[0]}，模型抓到 {total(True, 2)[0]}，"
              f"合计 {caught}，**漏检率 {(n_fab - caught) / n_fab:.0%}**",
              f"诚实样本 {n_ok} 条：误报 {false_alarm}（误报只会多问用户一次，代价比漏检小）\n", *rows]
    text = "\n".join(lines)
    print(text)
    with open(ROOT / "out" / "fabrication_report.md", "a", encoding="utf-8") as f:
        f.write(text + "\n")


if __name__ == "__main__":
    main()
