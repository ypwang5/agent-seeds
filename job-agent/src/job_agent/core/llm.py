"""模型适配层：业务代码只说"要快模型还是强模型"，不关心是哪家厂商。

模型用 "厂商:模型名" 表示，例如 deepseek:deepseek-flash、qwen:qwen-plus、
openrouter:anthropic/claude-sonnet-5.5、anthropic:claude-sonnet-5-5。
用环境变量切换：JOB_FAST_MODEL / JOB_STRONG_MODEL。Key 从项目根目录的 .env 读取。
"""
import os
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable
from pydantic import BaseModel

Role = Literal["fast", "strong"]

DEFAULTS: dict[Role, str] = {
    "fast": "deepseek:deepseek-flash",   # 解析、打分等量大的活
    "strong": "deepseek:deepseek-v4-pro",  # 改写、复核等需要判断力的活
}
DASHSCOPE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
OPENROUTER_URL = "https://openrouter.ai/api/v1"

# 读项目根目录的 .env；已经存在的环境变量优先（override=False），方便临时覆盖
load_dotenv(Path(__file__).parents[3] / ".env")


# 网页用户自带的 DeepSeek Key：只在本次请求的上下文里有效，不写进环境变量、不落盘、不打日志。
# ContextVar 会随 LangGraph 的并行任务一起复制过去，所以图里每个节点都能拿到同一个 Key。
USER_KEY: ContextVar[str | None] = ContextVar("user_deepseek_key", default=None)


def model_spec(role: Role) -> str:
    return os.environ.get(f"JOB_{role.upper()}_MODEL", DEFAULTS[role])


def get_model(spec: str) -> BaseChatModel:
    return _build(spec, USER_KEY.get() if spec.startswith("deepseek:") else None)


@lru_cache(maxsize=64)
def _build(spec: str, user_key: str | None) -> BaseChatModel:
    provider, name = spec.split(":", 1)
    common = {"temperature": 0, "max_retries": 2, "timeout": 120}
    if provider == "deepseek":
        # DeepSeek 默认开思考模式，而思考模式不支持结构化输出需要的 tool_choice（实测 400），统一关掉
        key = {"api_key": user_key} if user_key else {}
        return init_chat_model(f"deepseek:{name}", extra_body={"thinking": {"type": "disabled"}}, **key, **common)
    if provider == "qwen":
        # 通义千问走 OpenAI 兼容接口；同样关掉思考
        return init_chat_model(f"openai:{name}", base_url=DASHSCOPE_URL,
                               api_key=os.environ["DASHSCOPE_API_KEY"],
                               extra_body={"enable_thinking": False}, **common)
    if provider == "openrouter":
        # OpenRouter 用一个 Key 调多家模型（Claude 等），同样是 OpenAI 兼容接口
        return init_chat_model(f"openai:{name}", base_url=OPENROUTER_URL,
                               api_key=os.environ["OPENROUTER_API_KEY"], **common)
    if provider == "anthropic":
        return init_chat_model(f"anthropic:{name}", **common)
    raise ValueError(f"不支持的厂商：{provider}（可选 deepseek / qwen / openrouter / anthropic）")


def _structured_one(schema: type[BaseModel], spec: str) -> Runnable:
    # OpenRouter 上的 Claude（实测 Sonnet 5.5）不支持强制指定工具（tool_choice 报 400），改用 json_schema
    method = "json_schema" if spec.startswith("openrouter:anthropic/") else "function_calling"
    return get_model(spec).with_structured_output(schema, method=method).with_retry(stop_after_attempt=2)


def fallback_spec(spec: str) -> str | None:
    """主模型出错（服务挂了、限流）时换哪个模型。只给服务器自己的 Key 兜底：
    用户自带 Key 时不切，否则用户的请求会花服务器的通义 Key。选 qwen3.7-plus 依据见 evals/out/model_compare.md。"""
    fb = os.environ.get("JOB_FALLBACK_MODEL", "qwen:qwen3.7-plus")
    if not fb or fb == spec or USER_KEY.get() or not os.environ.get("DASHSCOPE_API_KEY"):
        return None
    return fb


def structured(schema: type[BaseModel], role: Role = "fast", spec: str | None = None) -> Runnable:
    """返回一个"输入提示词 → 输出 pydantic 对象"的 Runnable。

    默认用 function_calling：实测 DeepSeek 的 json_mode 不把字段定义发给模型，模型会自己编字段。
    with_retry：pydantic 校验失败或网络抖动时再试一次；仍失败就换备用模型（with_fallbacks）。
    """
    spec = spec or model_spec(role)
    runnable = _structured_one(schema, spec)
    if fb := fallback_spec(spec):
        runnable = runnable.with_fallbacks([_structured_one(schema, fb)])
    return runnable
