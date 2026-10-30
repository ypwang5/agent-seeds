"""MCP Server 离线测试：鉴权、Host 检查、工具列表与调用（调研函数换成假的）。"""
import anyio
from starlette.testclient import TestClient

from job_agent import mcp_server
from job_agent.job.research import CompanyReport
from job_agent.radar.competitor import CompetitorReport

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def test_token_required_and_host_checked():
    app = mcp_server.build_app("secret", ["127.0.0.1:8002"])
    with TestClient(app, base_url="http://127.0.0.1:8002") as c:
        assert c.post("/mcp", json=INIT, headers=HEADERS).status_code == 401
        assert c.post("/mcp", json=INIT, headers={**HEADERS, "authorization": "Bearer wrong"}).status_code == 401
        ok = c.post("/mcp", json=INIT, headers={**HEADERS, "authorization": "Bearer secret"})
        assert ok.status_code == 200
        # Host 不在白名单：防 DNS 重绑定。会话管理器一个进程只能启动一次，所以在同一个客户端里换 Host 头
        r = c.post("/mcp", json=INIT, headers={**HEADERS, "authorization": "Bearer secret", "host": "evil.example.com"})
        assert r.status_code in (400, 421)


def test_tools_listed_and_callable(monkeypatch):
    monkeypatch.setattr(mcp_server, "_research_competitors",
                        lambda topic: CompetitorReport(topic=topic, summary="已有 A、B 两款", competitors=["A", "B"]))
    monkeypatch.setattr(mcp_server, "_research_company",
                        lambda company, title: CompanyReport(company=company, summary="做金融科技"))

    async def run():
        names = {t.name for t in await mcp_server.mcp.list_tools()}
        assert names == {"research_company", "research_competitors"}
        return await mcp_server.mcp.call_tool("research_competitors", {"topic": "记账"})

    result = anyio.run(run)
    text = str(result)
    assert "竞品调研：记账" in text and "已有 A、B 两款" in text
