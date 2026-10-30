"""真实调用模型的测试：uv run pytest -m live（没有对应 Key 会自动跳过）。"""
import os
from pathlib import Path

import pytest

from job_agent.job.parse import parse_jd  # 导入时会加载 .env，下面才能读到 Key

pytestmark = pytest.mark.live
JD = (Path(__file__).parents[1] / "evals" / "jds" / "00_示例_合成.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize("spec, key", [
    ("deepseek:deepseek-flash", "DEEPSEEK_API_KEY"),
    ("qwen:qwen-plus", "DASHSCOPE_API_KEY"),
    ("openrouter:anthropic/claude-haiku-5.5", "OPENROUTER_API_KEY"),
])
def test_parse_jd_each_provider(spec, key, monkeypatch):
    if not os.environ.get(key):
        pytest.skip(f"未设置 {key}")
    monkeypatch.setenv("JOB_FAST_MODEL", spec)
    jd = parse_jd(JD)
    assert "AI" in jd.title
    assert jd.years_required == 3
    assert any("驻场" in s for s in jd.employment_signals)
