"""岗位筛选评测：一份简历 × 全部 JD 跑一次筛选图（就是用户的真实用法），看排序、风险标签和耗时。

运行：uv run python evals/run_screen_eval.py [--concurrency 4]
- 风险标签按类型对比 evals/risk_labels.json（只有 7 个 JD 有标注）：多报 = 标注没有、模型报了；漏报 = 反过来
- 结果追加到 evals/out/screen_report.md
"""
import argparse
import json
import time
from datetime import datetime
from pathlib import Path

from job_agent.core.pii import Masker
from job_agent.job.parse import number_lines, read_text
from job_agent.job.screen_graph import build_graph

ROOT = Path(__file__).parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=4, help="同时处理几个 JD")
    args = ap.parse_args()
    labels = {k: set(v) for k, v in json.loads((ROOT / "risk_labels.json").read_text(encoding="utf-8")).items()
              if not k.startswith("_")}
    lines = number_lines(Masker().mask(read_text(next((ROOT / "resumes").glob("*.pdf")))))   # 真实简历不进仓库
    jds = [{"id": p.stem, "text": p.read_text(encoding="utf-8")} for p in sorted((ROOT / "jds").glob("*.txt"))]

    t0, started, done = time.time(), {}, {}
    final = None
    for mode, chunk in build_graph().stream({"lines": lines, "jds": jds}, {"max_concurrency": args.concurrency},
                                            stream_mode=["custom", "values"]):
        if mode == "custom":
            started.setdefault(chunk["id"], time.time())
            if chunk["step"] in ("完成", "失败"):
                done[chunk["id"]] = time.time() - started[chunk["id"]]
                print(f"[{len(done)}/{len(jds)}] {chunk['id']} {chunk['step']}")
        else:
            final = chunk
    wall = time.time() - t0

    out = [f"\n## {datetime.now():%Y-%m-%d %H:%M} · {len(jds)} 个 JD，并发 {args.concurrency}\n",
           f"总耗时 {wall:.0f}s；单个 JD 耗时之和 {sum(done.values()):.0f}s（并行省下的就是两者之差）\n",
           "| # | JD | 建议 | 匹配分 | 风险 | 对比标注 | 校验问题 |", "|---|---|---|---|---|---|---|"]
    tp = fp = fn = 0
    for i, r in enumerate(final["ranked"], 1):
        if "error" in r:
            out.append(f"| {i} | {r['id']} | 失败 | | {r['error'][:60]} | | |")
            continue
        got = {x["type"] for x in r["risks"]} - {"公司负面"}
        cmp = ""
        if r["id"] in labels:
            exp = labels[r["id"]]
            tp, fp, fn = tp + len(got & exp), fp + len(got - exp), fn + len(exp - got)
            cmp = "对" if got == exp else "；".join(filter(None, [
                f"多报 {'、'.join(sorted(got - exp))}" if got - exp else "",
                f"漏报 {'、'.join(sorted(exp - got))}" if exp - got else ""]))
        risks = "<br>".join(f"{x['type']}：{x['detail']}" for x in r["risks"]) or "无"
        out.append(f"| {i} | {r['id']} | {r['advice']} | {r['score']:.0%} | {risks} | {cmp} | {len(r['problems'])} |")
    out.append(f"\n风险类型（有标注的 7 个 JD）：对 {tp}，多报 {fp}，漏报 {fn}；"
               f"精确率 {tp / max(tp + fp, 1):.0%}，召回率 {tp / max(tp + fn, 1):.0%}")
    text = "\n".join(out)
    print(text)
    (ROOT / "out").mkdir(exist_ok=True)
    with open(ROOT / "out" / "screen_report.md", "a", encoding="utf-8") as f:
        f.write(text + "\n")
    (ROOT / "out" / "screen_last.json").write_text(json.dumps(final["ranked"], ensure_ascii=False, indent=1),
                                                    encoding="utf-8")


if __name__ == "__main__":
    main()
