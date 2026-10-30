"""内核 MCP Server：把"带来源核对的调研 Agent"作为工具开放给别的应用（Dify 需求发现工作流、Claude Desktop 等）。

    uv run job-mcp            # 默认 127.0.0.1:8002，路径 /mcp（Streamable HTTP）

MCP（Model Context Protocol）是"AI 应用调用外部工具"的标准协议：工具在这里写一次，
任何支持 MCP 的客户端都能发现并调用，不用为每个平台单独写插件。

工具：
- research_company：公司调研（求职视角：业务、规模、技术、风险、面经）
- research_competitors：竞品调研（独立开发者视角：现有产品、定价、用户抱怨、没被满足的地方）
两者都是 ReAct 子图，搜索次数封顶，每条事实的来源链接由程序核对（必须是本次搜索真实返回过的）。

鉴权：每次调用都花搜索费和模型费，必须带 Authorization: Bearer <MCP_TOKEN>（.env 里配置）；没配 MCP_TOKEN 拒绝启动。
"""
import argparse
import hmac
import os

import anyio
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from job_agent.job.research import report_markdown as company_markdown
from job_agent.job.research import research_company as _research_company
from job_agent.radar.competitor import report_markdown as competitor_markdown
from job_agent.radar.competitor import research_competitors as _research_competitors

mcp = MCPServer(
    "job-agent-kernel",
    instructions="调研工具：每条事实都附来源链接，且链接由程序核对过。一次调用约 30～90 秒（会联网搜索），结果缓存 7 天。")


@mcp.tool()
async def research_company(company: str, job_title: str = "") -> dict:
    """调研一家公司（求职视角）：主营业务、规模与融资、技术与 AI 投入、近期动态、风险信号、面试经验。
    每条事实带来源链接。company 用公司全称或常用名；job_title 可选，填应聘岗位能让调研更聚焦。"""
    r = await anyio.to_thread.run_sync(lambda: _research_company(company, job_title))
    return {"markdown": company_markdown(r), "report": r.model_dump()}


@mcp.tool()
async def research_competitors(topic: str) -> dict:
    """调研一个需求方向已有的竞品：有哪些产品、定价、用户评价与抱怨、现有产品没解决好的地方（gaps）。
    每条事实带来源链接。topic 用一句话描述需求或产品方向，例如"程序员求职简历优化工具"。"""
    r = await anyio.to_thread.run_sync(lambda: _research_competitors(topic))
    return {"markdown": competitor_markdown(r), "report": r.model_dump()}


class BearerAuth:
    """最简单的鉴权：请求头必须带对的 Token。用 compare_digest 防按时间差猜 Token。"""

    def __init__(self, app, token: str):
        self.app, self.expected = app, f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            got = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(got, self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
                await send({"type": "http.response.body", "body": "缺少或错误的 MCP Token".encode()})
                return
        await self.app(scope, receive, send)


def build_app(token: str, allowed_hosts: list[str]):
    # 防 DNS 重绑定：只接受这些 Host 头。Dify 在 Docker 里访问本机用的是 host.docker.internal
    security = TransportSecuritySettings(allowed_hosts=allowed_hosts, allowed_origins=[])
    return BearerAuth(mcp.streamable_http_app(transport_security=security), token)


def main():
    ap = argparse.ArgumentParser(prog="job-mcp", description="求职 Agent 内核 MCP Server")
    ap.add_argument("--host", default="127.0.0.1", help="Dify 在 Docker 里要访问时用 0.0.0.0")
    ap.add_argument("--port", type=int, default=8002)
    ap.add_argument("--public-host", action="append", default=[],
                    help="经反向代理对外时的域名（如 job.wyphub.com），可写多次；防 DNS 重绑定只放行这些 Host")
    args = ap.parse_args()
    token = os.environ.get("MCP_TOKEN")
    if not token:
        raise SystemExit("请先在 .env 里设置 MCP_TOKEN（任意一串足够长的随机字符）")
    hosts = [f"{h}:{args.port}" for h in ("127.0.0.1", "localhost", "host.docker.internal")] + args.public_host
    uvicorn.run(build_app(token, hosts), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
