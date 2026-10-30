"""命令行：深度准备。

    uv run job-prep --resume 简历.pdf --jd JD.txt
    uv run job-prep --resume 简历.pdf --jd JD.txt --thread <上次打印的编号>   # 中断后接着跑

流程中途如果有改写没通过防编造校验，会在终端逐条问你：a 采纳 / e 修改 / r 拒绝（默认）。
存档在 prep_out/checkpoints.sqlite；报告写到 prep_out/<thread>.md（个人信息已还原，只在本机）。
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from job_agent.core.pii import Masker
from job_agent.job.parse import number_lines, read_text
from job_agent.job.prep_graph import build_graph

OUT = Path("prep_out")


def ask_review(payload: dict, auto: str | None) -> dict:
    answers = {}
    print(f"\n有 {len(payload['items'])} 条改写没通过防编造校验，需要你确认：")
    for it in payload["items"]:
        print("\n" + "-" * 60)
        print(f"原文：{it['original']}\n改写：{it['new_text']}")
        for p in it["rule_problems"]:
            print(f"  程序检查：{p}")
        if it["verdict"] != "通过":
            print(f"  模型复核：{it['verdict']}，{it['reason']}")
        if auto:
            choice = auto[0]
            print(f"（--auto {auto}）")
        else:
            choice = (input("a 采纳 / e 修改 / r 拒绝 [r]：").strip().lower() or "r")[:1]
        if choice == "a":
            answers[str(it["index"])] = {"action": "accept"}
        elif choice == "e":
            answers[str(it["index"])] = {"action": "edit", "text": input("改成：").strip()}
        else:
            answers[str(it["index"])] = {"action": "reject"}
    return answers


def main(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(prog="job-prep", description="求职 Agent：深度准备（差距分析、公司调研、简历改写、面试题）")
    ap.add_argument("--resume", required=True, help="简历文件（PDF / txt / md）")
    ap.add_argument("--jd", required=True, help="JD 文本文件")
    ap.add_argument("--thread", help="接着跑上次中断的任务")
    ap.add_argument("--auto", choices=["accept", "reject"], help="不逐条询问，统一采纳或拒绝（测试用）")
    args = ap.parse_args(argv)

    # 每次都从文件重新脱敏：同样的简历得到同样的占位符映射，所以中断后也能还原，且存档里不含个人信息
    masker = Masker()
    lines = number_lines(masker.mask(read_text(args.resume)))
    thread = args.thread or f"{Path(args.jd).stem}_{datetime.now():%m%d%H%M%S}"
    config = {"configurable": {"thread_id": thread}}
    OUT.mkdir(exist_ok=True)

    with SqliteSaver.from_conn_string(str(OUT / "checkpoints.sqlite")) as saver:
        graph = build_graph(saver)
        if args.thread and graph.get_state(config).next:
            print(f"接着跑任务 {thread}，停在：{graph.get_state(config).next}")
            inp = None
        else:
            print(f"任务编号：{thread}（中断后用 --thread {thread} 接着跑）")
            inp = {"jd_text": Path(args.jd).read_text(encoding="utf-8"), "lines": lines, "problems": []}
        while True:
            for chunk in graph.stream(inp, config, stream_mode="updates"):
                for node in chunk:
                    if node != "__interrupt__":
                        print(f"  完成：{node}")  # 不用 ✔：Windows 控制台默认 GBK 打印不了
            state = graph.get_state(config)
            if not state.next:
                break
            payload = state.tasks[0].interrupts[0].value  # 本图只有一个人工确认点
            inp = Command(resume=ask_review(payload, args.auto))

    report = masker.unmask(state.values["report_md"])
    path = OUT / f"{thread}.md"
    path.write_text(report, encoding="utf-8")
    print(f"\n报告：{path.resolve()}")


if __name__ == "__main__":
    sys.exit(main())
