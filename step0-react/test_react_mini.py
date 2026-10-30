"""离线测试：用"假模型"按剧本返回结果，验证循环逻辑本身（不需要 API Key）。

运行：uv run --with pytest --with openai pytest -q
"""
import json
from types import SimpleNamespace

from openai.types.chat import ChatCompletionMessage

from react_mini import MAX_STEPS, calculator, react, read_file


def tool_msg(name, args, call_id="call_1"):
    return ChatCompletionMessage.model_validate({"role": "assistant", "content": None, "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]})


def answer_msg(text):
    return ChatCompletionMessage.model_validate({"role": "assistant", "content": text})


class FakeClient:
    """按顺序返回剧本里的消息，并记录每次收到的 messages。"""

    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append([dict(m) for m in kw["messages"]])
        msg = self.script.pop(0) if self.script else self.last
        self.last = msg
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def test_tool_result_is_fed_back_then_answer():
    client = FakeClient([tool_msg("calculator", {"expression": "(2+3)*4"}), answer_msg("结果是 20")])
    assert react("算 (2+3)*4", client, "fake") == "结果是 20"
    second_round = client.calls[1]
    assert second_round[-1] == {"role": "tool", "tool_call_id": "call_1", "content": "20"}


def test_stops_at_max_steps():
    client = FakeClient([tool_msg("today", {})])  # 模型永远要工具
    assert "超过" in react("死循环", client, "fake")
    assert len(client.calls) == MAX_STEPS


def test_tool_errors_are_returned_not_raised():
    client = FakeClient([tool_msg("no_such_tool", {}), answer_msg("好的")])
    react("x", client, "fake")
    assert "工具执行出错" in client.calls[1][-1]["content"]


def test_read_file_cannot_escape_data_dir():
    assert "只能读取 data 目录" in read_file("../react_mini.py")
    assert "云帆" in read_file("resume.txt")


def test_calculator_rejects_code():
    assert calculator("(2026-2021)*12+10") == "70"
    try:
        calculator("__import__('os').system('dir')")
        assert False, "应该拒绝"
    except ValueError:
        pass
