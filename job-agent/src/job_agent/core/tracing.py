"""执行轨迹（Langfuse）：可选。.env 里配了 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST 才开启。

用法：把 tracing_config(会话编号, 轨迹名) 合并进 graph.invoke / stream 的 config。
LangChain 的回调会随 config 传给图里每个节点、每次模型调用，所以一个回调就能记下整张图的执行轨迹。
记录的内容都是脱敏后的（简历进图前已经换成占位符）。
"""
import os
from functools import lru_cache

import job_agent.core.llm  # noqa: F401  确保 .env 已加载


def enabled() -> bool:
    return bool(os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"))


@lru_cache
def _handler():
    from langfuse.langchain import CallbackHandler  # 没配 Key 时不导入，省启动时间
    return CallbackHandler()


def tracing_config(session_id: str, name: str) -> dict:
    """session_id 把同一个任务的多次请求（比如深度准备的暂停和恢复）归到一起；name 是轨迹名。"""
    if not enabled():
        return {}
    return {"callbacks": [_handler()], "run_name": name, "metadata": {"langfuse_session_id": session_id}}
