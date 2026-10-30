"""JD 截图 → 文本（多模态模型转写）。

先转成文字再走 parse_jd，而不是让多模态模型直接抽结构：后面的风险依据、差距依据都要和"JD 原文"核对，
得先有一份文字原文。同一个 JD 的多张截图一次发给模型，按顺序拼成一份。
"""
import base64
import os

from langchain_core.messages import HumanMessage

from job_agent.core.llm import get_model

VISION_DEFAULT = "qwen:qwen3-vl-plus"  # DeepSeek 不支持图片；qwen-vl-max-latest 实测 403 无权限
PROMPT = "下面是同一个招聘岗位的截图（按顺序）。把其中的招聘信息原样转写成纯文本：不总结、不遗漏、不添加；" \
         "忽略 App 界面元素（按钮、导航栏、推荐岗位）和招聘者个人信息（姓名、头像、活跃状态）。"


def ocr_jd(images: list[tuple[bytes, str]]) -> str:
    """images: [(图片字节, MIME 类型如 image/png)]，返回转写后的 JD 文本。"""
    content = [{"type": "text", "text": PROMPT}] + [
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}}
        for data, mime in images]
    spec = os.environ.get("JOB_VISION_MODEL", VISION_DEFAULT)
    return get_model(spec).invoke([HumanMessage(content=content)]).content.strip()
