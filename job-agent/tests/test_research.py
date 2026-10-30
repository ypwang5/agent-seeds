"""公司调研的离线测试：只测程序校验，不调用模型和搜索。"""
from job_agent.job.research import CompanyReport, Fact, research_company, verify_sources


def test_verify_sources_drops_made_up_links():
    report = CompanyReport(company="某公司", summary="", facts=[
        Fact(category="业务", content="做证券经纪", source_url="https://a.com/1"),
        Fact(category="风险信号", content="2025 年裁员 30%", source_url="https://made-up.com/x"),
    ])
    out = verify_sources(report, seen={"https://a.com/1", "https://b.com/2"})
    assert [f.content for f in out.facts] == ["做证券经纪"]
    assert len(out.problems) == 1 and "made-up.com" in out.problems[0]


def test_unknown_company_skips_search():
    report = research_company(None)
    assert report.facts == [] and "公司名称" in report.not_found


def test_search_falls_back_when_engine_returns_empty(monkeypatch):
    from job_agent.core import search as S
    calls = []

    class R:
        def __init__(self, data): self.data = data
        def raise_for_status(self): pass
        def json(self): return self.data

    def fake_post(url, timeout, headers, json):
        calls.append(json["search_engine"])
        return R({"search_result": []} if json["search_engine"] == "search_std" else
                 {"search_result": [{"title": "t", "link": "https://x", "content": "c"}]})

    monkeypatch.setenv("JOB_SEARCH", "zhipu")
    monkeypatch.setenv("ZHIPUAI_API_KEY", "k")
    monkeypatch.delenv("JOB_ZHIPU_ENGINES", raising=False)
    monkeypatch.setattr(S.httpx, "post", fake_post)
    assert [h.url for h in S.search("q")] == ["https://x"]
    assert calls == ["search_std", "search_pro_sogou"]


def test_empty_research_not_cached(monkeypatch, tmp_path):
    from job_agent.job import research as R
    monkeypatch.setattr(R, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(R, "run_research", lambda *a, **k: CompanyReport(company="某公司", summary="没搜到"))
    R.research_company("某公司", use_cache=False)
    assert not list(tmp_path.iterdir())
