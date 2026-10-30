# /// script
# requires-python = ">=3.12"
# dependencies = ["openai>=1.50"]
# ///
"""第 0 步：手写最小 ReAct 循环（不用任何 Agent 框架）。

本质：一个 while 循环 ——
  调模型 → 模型要工具？执行工具，把结果塞回 messages，再调模型 ；不要工具？那就是最终答案。

运行：uv run react_mini.py "你的问题"
"""
import ast
import datetime
import json
import operator
import os
import sys
from pathlib import Path

from openai import OpenAI

DATA_DIR = Path(__file__).parent / "data"  # 工具只能读这个目录（最小权限）
MAX_STEPS = 8  # 步数上限：防止模型陷入死循环、烧钱


# ---------- 1. 工具：就是普通的 Python 函数，返回字符串 ----------
def read_file(name: str) -> str:
    path = (DATA_DIR / name).resolve()
    if DATA_DIR.resolve() not in path.parents:
        return "错误：只能读取 data 目录下的文件"
    if not path.exists():
        return f"错误：文件不存在。可用文件：{[p.name for p in DATA_DIR.iterdir()]}"
    return path.read_text(encoding="utf-8")


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.USub: operator.neg}


def calculator(expression: str) -> str:
    # 不用 eval()：模型给的字符串不可信，只允许数字和 + - * /
    def ev(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.operand))
        raise ValueError("只支持数字和 + - * /")
    return str(ev(ast.parse(expression, mode="eval").body))


def today() -> str:
    return datetime.date.today().isoformat()


TOOLS = {"read_file": read_file, "calculator": calculator, "today": today}


# ---------- 2. 工具说明书（JSON Schema）：模型只看得到这个，看不到函数代码 ----------
def _schema(name, desc, params=None):
    params = params or {}
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": params, "required": list(params)}}}


TOOL_SCHEMAS = [
    _schema("read_file", "读取 data 目录下的文本文件",
            {"name": {"type": "string", "description": "文件名，如 resume.txt"}}),
    _schema("calculator", "计算四则运算表达式",
            {"expression": {"type": "string", "description": "如 (2026-2021)*12+10"}}),
    _schema("today", "返回今天的日期，格式 YYYY-MM-DD"),
]


# ---------- 3. 执行一次工具调用：出错也返回文字，让模型看到错误后自己修正 ----------
def run_tool(call) -> str:
    try:
        args = json.loads(call.function.arguments or "{}")  # 参数是模型生成的 JSON 字符串，可能不合法
        return TOOLS[call.function.name](**args)
    except Exception as e:
        return f"工具执行出错：{type(e).__name__}: {e}"


# ---------- 4. ReAct 循环本体 ----------
def react(question: str, client, model: str) -> str:
    # messages 就是 Agent 的全部"状态"：问题、每一步的工具调用和结果都在这里
    messages = [
        {"role": "system", "content": "你是求职助手。需要信息就调用工具，不要编造；信息够了再回答。"},
        {"role": "user", "content": question},
    ]
    for step in range(1, MAX_STEPS + 1):
        resp = client.chat.completions.create(
            model=model, messages=messages, tools=TOOL_SCHEMAS,
            extra_body={"thinking": {"type": "disabled"}})  # 关掉思考模式：更快，也看得清循环
        msg = resp.choices[0].message
        if not msg.tool_calls:  # 模型不再要工具 → 这就是最终答案，循环结束
            return msg.content
        if msg.content:
            print(f"[第{step}步] 模型说：{msg.content}")
        messages.append(msg.model_dump(exclude_none=True))  # Reason：记下"模型决定调哪些工具"
        for call in msg.tool_calls:
            result = run_tool(call)  # Act：执行工具
            print(f"[第{step}步] 调用 {call.function.name}({call.function.arguments}) → {result[:60]!r}")
            # Observe：结果用 role=tool 塞回去，tool_call_id 对应是哪一次调用
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
    return f"超过 {MAX_STEPS} 步仍未完成，已停止。"


if __name__ == "__main__":
    client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
    question = sys.argv[1] if len(sys.argv) > 1 else \
        "读一下 resume.txt，算算我在云帆一共干了多少个月？到今天为止我离职多少天了？"
    print("\n最终回答：", react(question, client, model))
