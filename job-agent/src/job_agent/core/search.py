"""搜索适配层：业务代码只调 search(query)，不关心是哪家搜索服务。

用环境变量 JOB_SEARCH 切换：zhipu（智谱 Web Search，复用 ZHIPUAI_API_KEY）或 tavily（需要 TAVILY_API_KEY）。
没设置时：有 Tavily Key 用 Tavily，否则用智谱。
"""
import logging
import os
from dataclasses import dataclass

import httpx

import job_agent.core.llm  # noqa: F401  确保 .env 已加载

ZHIPU_URL = "https://open.bigmodel.cn/api/paas/v4/web_search"
TAVILY_URL = "https://api.tavily.com/search"
log = logging.getLogger(__name__)


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str
    date: str | None = None


def provider() -> str:
    return os.environ.get("JOB_SEARCH") or ("tavily" if os.environ.get("TAVILY_API_KEY") else "zhipu")


def search(query: str, count: int = 5) -> list[SearchHit]:
    if provider() == "tavily":
        r = httpx.post(TAVILY_URL, timeout=30, headers={"Authorization": f"Bearer {os.environ['TAVILY_API_KEY']}"},
                       json={"query": query, "max_results": count})
        r.raise_for_status()
        return [SearchHit(x["title"], x["url"], x.get("content", ""), x.get("published_date"))
                for x in r.json().get("results", [])]
    # 智谱：按顺序试引擎，返回空就换下一个。实测 2026-10-29 search_std、search_pro 对任何查询都返回 200 + 空结果，
    # 而 search_pro_sogou 正常——"成功但为空"不会报错，不兜底的话调研会静默变成"什么都没查到"
    engines = os.environ.get("JOB_ZHIPU_ENGINES", "search_std,search_pro_sogou").split(",")
    for engine in engines:
        r = httpx.post(ZHIPU_URL, timeout=30, headers={"Authorization": f"Bearer {os.environ['ZHIPUAI_API_KEY']}"},
                       json={"search_query": query[:70], "search_engine": engine.strip(), "search_intent": False,
                             "count": count, "content_size": "medium"})
        r.raise_for_status()
        hits = [SearchHit(x.get("title", ""), x.get("link", ""), x.get("content", ""), x.get("publish_date"))
                for x in r.json().get("search_result") or []]
        if hits:
            return hits
        log.warning("智谱搜索引擎 %s 返回空结果，换下一个", engine)
    return []
