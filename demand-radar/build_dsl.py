"""生成"需求雷达"Dify 工作流的 DSL（YAML）。

    uv run --no-project --with pyyaml python build_dsl.py   →   dist/demand_radar.yml

节点格式参照 Dify 1.18（与本机 Dify 实例一致）。代码节点的源码来自 code_nodes.py，提示词来自 prompts.py，
这样本地单测、评测和线上跑的是同一份代码。
"""
import inspect
import re
from pathlib import Path

import yaml

import code_nodes as C
import prompts as P

FLASH = {"provider": "langgenius/deepseek/deepseek", "name": "deepseek-v4-flash", "mode": "chat"}
PRO = {"provider": "langgenius/deepseek/deepseek", "name": "deepseek-v4-pro", "mode": "chat"}
MCP_PROVIDER = "job-kernel"   # 在 Dify「工具 → MCP」里添加内核 MCP Server 时填的服务器标识
RETRY = {"enabled": True, "max_retries": 2, "retry_interval": 2000,
         "exponential_backoff": {"enabled": False, "multiplier": 2, "max_interval": 10000}}


class Graph:
    def __init__(self):
        self.nodes, self.edges, self.types = [], [], {}

    def add(self, nid: str, data: dict, x: int, y: int = 200, parent: str = "", **extra):
        """parent：放进哪个迭代节点里（坐标相对于迭代节点）。"""
        self.types[nid] = data["type"]
        if parent:
            data = {**data, "isInIteration": True, "isInLoop": False, "iteration_id": parent}
            px, py = next((n["position"]["x"], n["position"]["y"]) for n in self.nodes if n["id"] == parent)
            extra = {"parentId": parent, "zIndex": 1002, **extra}
        else:
            px = py = 0
        self.nodes.append({
            "id": nid, "type": "custom", "data": {"selected": False, "desc": "", **data},
            "position": {"x": x, "y": y}, "positionAbsolute": {"x": px + x, "y": py + y},
            "width": 244, "height": 90, "selected": False, "sourcePosition": "right", "targetPosition": "left",
            **extra})

    def link(self, src: str, dst: str, iteration: str = ""):
        data = {"sourceType": self.types[src], "targetType": self.types[dst],
                "isInIteration": bool(iteration), "isInLoop": False}
        if iteration:
            data["iteration_id"] = iteration
        self.edges.append({
            "id": f"{src}-source-{dst}", "type": "custom", "source": src, "target": dst,
            "sourceHandle": "source", "targetHandle": "target", "zIndex": 1002 if iteration else 0, "data": data})


def code(title, fn, inputs: dict, outputs: dict):
    src = re.sub(rf"^def {fn.__name__}\(", "def main(", inspect.getsource(fn), count=1)
    return {"type": "code", "title": title, "code_language": "python3", "code": src,
            "variables": [{"variable": k, "value_selector": v} for k, v in inputs.items()],
            "outputs": {k: {"type": t, "children": None} for k, t in outputs.items()}}


def llm(title, model, system, user, params):
    return {"type": "llm", "title": title, "model": {**model, "completion_params": params},
            "prompt_template": [{"role": "system", "text": system}, {"role": "user", "text": user}],
            "context": {"enabled": False, "variable_selector": []}, "vision": {"enabled": False},
            "memory": None, "structured_output_enabled": False, "reasoning_format": "separated",
            "retry_config": RETRY}


def http_get(title, url, params, headers=""):
    return {"type": "http-request", "title": title, "method": "get", "url": url,
            "authorization": {"type": "no-auth", "config": None}, "headers": headers, "params": params,
            "body": {"type": "none", "data": []},
            "timeout": {"connect": 10, "read": 30, "write": 30,
                        "max_connect_timeout": 0, "max_read_timeout": 0, "max_write_timeout": 0},
            "ssl_verify": True, "retry_config": RETRY,
            # 某个来源挂了不影响其他来源：失败时输出空结果，继续往下走
            "error_strategy": "default-value",
            "default_value": [{"key": "body", "type": "string", "value": "{}"},
                              {"key": "status_code", "type": "number", "value": 0}]}


def build():
    g = Graph()
    g.add("start", {"type": "start", "title": "开始", "variables": [
        {"variable": "topic", "label": "领域（如：记账、简历工具、家庭网络）", "type": "text-input",
         "required": True, "max_length": 50, "options": []},
        {"variable": "months", "label": "时间范围（近几个月，默认 12）", "type": "number",
         "required": False, "options": []}]}, 40)
    g.add("llm_q", llm("扩展检索词", FLASH, P.QUERIES_SYSTEM, P.QUERIES_USER, {"temperature": 0.2}), 340)
    g.add("q", code("解析检索词", C.parse_queries, {"llm_text": ["llm_q", "text"], "topic": ["start", "topic"]},
                    {"en": "string", "zh": "string"}), 640)
    g.add("win", code("时间窗", C.time_window, {"months": ["start", "months"]},
                      {"months": "number", "since_ts": "number", "since_date": "string",
                       "alive_ts": "number", "alive_date": "string"}), 640, 360)
    # 第一轮（并行）：时间窗内的新帖 + 老热帖候选 + App Store 搜 App。
    # 只按热度/相关度排，搜出来多是 2012～2020 年的老帖（实测 V2EX 30 条里几乎全是）
    gh_headers = "User-Agent:demand-radar\nAccept:application/vnd.github+json"
    g.add("hn", http_get("HN 新帖", "https://hn.algolia.com/api/v1/search",
                         "query:{{#q.en#}}\ntags:(story,comment)\nhitsPerPage:24\n"
                         "numericFilters:created_at_i>{{#win.since_ts#}}"), 940, -280)
    g.add("gh", http_get("GitHub 新 issue", "https://api.github.com/search/issues",
                         # 只在标题里匹配、按相关度排：按点赞排序会搜出全站热门的无关 issue（实测"记账"搜出 Go 语言提案）
                         "q:{{#q.en#}} in:title is:issue created:>{{#win.since_date#}}\nper_page:24", gh_headers), 940, -120)
    g.add("v2", http_get("V2EX 新帖（sov2ex 搜索）", "https://www.sov2ex.com/api/search",
                         "q:{{#q.zh#}}\nsize:24\nsort:sumup\ngte:{{#win.since_ts#}}"), 940, 40)
    g.add("gh_old", http_get("GitHub 老热 issue", "https://api.github.com/search/issues",
                             # 时间窗之前创建、近半年有更新、评论多的
                             "q:{{#q.en#}} in:title is:issue created:<{{#win.since_date#}} updated:>{{#win.alive_date#}} "
                             "comments:>=5\nsort:comments\nper_page:6", gh_headers), 940, 200)
    g.add("v2_old", http_get("V2EX 老热帖", "https://www.sov2ex.com/api/search",
                             "q:{{#q.zh#}}\nsize:8\nsort:sumup\nlte:{{#win.since_ts#}}"), 940, 360)
    g.add("as_search", http_get("App Store 搜 App", "https://itunes.apple.com/search",
                                "term:{{#q.zh#}}\ncountry:cn\nentity:software\nlimit:3"), 940, 520)
    # 苹果搜索接口按"附件"返回（content-disposition: attachment），Dify 存成文件、body 为空，用文档提取器读回文本
    g.add("as_text", {"type": "document-extractor", "title": "读取 App 搜索结果", "variable_selector": ["as_search", "files"],
                      "is_array_file": True, "error_strategy": "default-value",
                      "default_value": [{"key": "text", "type": "array[string]", "value": []}]}, 1240, 560)
    g.add("plan", code("第二轮抓取清单", C.plan_followups,
                       {"gh_old": ["gh_old", "body"], "v2_old": ["v2_old", "body"], "as_search": ["as_search", "body"],
                        "alive_date": ["win", "alive_date"], "as_text": ["as_text", "text"]},
                       {"urls": "array[string]", "meta": "string"}), 1240, 360)
    # 第二轮：逐个抓老热帖的回复 / App 的评论（迭代节点，5 个并行）
    g.add("follow", {"type": "iteration", "title": "抓回复和评论", "start_node_id": "followstart",
                     "iterator_selector": ["plan", "urls"], "iterator_input_type": "array[string]",
                     "output_selector": ["follow_http", "body"], "output_type": "array[string]",
                     "is_parallel": True, "parallel_nums": 5, "error_handle_mode": "continue-on-error",
                     "flatten_output": False, "width": 388, "height": 178}, 1540, 360,
          width=388, height=178, zIndex=1)
    g.add("followstart", {"type": "iteration-start", "title": ""}, 24, 68, parent="follow",
          type="custom-iteration-start", draggable=False, selectable=False, width=44, height=48)
    g.add("follow_http", http_get("GET", "{{#follow.item#}}", "", "User-Agent:demand-radar"), 128, 68,
          parent="follow")
    g.add("merge", code("清洗去重", C.merge_posts,
                        {"hn_body": ["hn", "body"], "gh_body": ["gh", "body"], "v2_body": ["v2", "body"],
                         "meta": ["plan", "meta"], "bodies": ["follow", "output"],
                         "since_ts": ["win", "since_ts"], "alive_ts": ["win", "alive_ts"]},
                        {"posts": "string", "listing_hn": "string", "listing_gh": "string", "listing_v2": "string",
                         "listing_as": "string", "count": "number", "source_counts": "string",
                         "old_count": "number", "apps": "string"}), 2040)
    for i, src in enumerate(["hn", "gh", "v2", "as"]):   # 按来源并行判断（一次判 90 条实测 114 秒）
        g.add(f"cls_{src}", llm(f"判断需求（{src.upper()}）", FLASH, P.CLASSIFY_SYSTEM,
                                "{{#merge.listing_" + src + "#}}", {"temperature": 0}), 2340, -40 + 160 * i)
    g.add("verify", code("核对原话", C.verify_needs, {"llm_hn": ["cls_hn", "text"], "llm_gh": ["cls_gh", "text"],
                                                    "llm_v2": ["cls_v2", "text"], "llm_as": ["cls_as", "text"],
                                                    "posts": ["merge", "posts"]},
                         {"needs": "string", "listing": "string", "need_count": "number",
                          "dropped": "number", "classified": "number"}), 2640)
    g.add("llm_cluster", llm("归纳痛点", PRO, P.CLUSTER_SYSTEM, P.CLUSTER_USER, {"temperature": 0}), 2940)
    g.add("fin", code("校验与打分", C.finalize_points,
                      {"llm_text": ["llm_cluster", "text"], "needs": ["verify", "needs"], "topic": ["start", "topic"]},
                      {"points": "string", "point_count": "number", "competitor_topic": "string"}), 3240)
    g.add("comp", {
        "type": "tool", "title": "竞品调研（内核 MCP）", "provider_id": MCP_PROVIDER, "provider_type": "mcp",
        "provider_name": MCP_PROVIDER, "tool_name": "research_competitors", "tool_label": "research_competitors",
        "tool_configurations": {},
        "tool_parameters": {"topic": {"type": "mixed", "value": "{{#fin.competitor_topic#}}"}},
        "error_strategy": "default-value",
        "default_value": [{"key": "text", "type": "string", "value": "（竞品调研服务暂不可用）"}]}, 3540)
    g.add("report", code("生成报告", C.render_report, {
        "points": ["fin", "points"], "comp_json": ["comp", "json"], "comp_text": ["comp", "text"], "topic": ["start", "topic"],
        "source_counts": ["merge", "source_counts"], "post_count": ["merge", "count"],
        "need_count": ["verify", "need_count"], "dropped": ["verify", "dropped"], "months": ["win", "months"],
        "old_count": ["merge", "old_count"], "apps": ["merge", "apps"]}, {"report": "string"}), 3840)
    g.add("end", {"type": "end", "title": "结束", "outputs": [
        {"variable": "report", "value_selector": ["report", "report"], "value_type": "string"}]}, 4140)

    first = ["hn", "gh", "v2", "gh_old", "v2_old", "as_search"]
    for a, b in ([("start", "llm_q"), ("llm_q", "q"), ("start", "win")] + [(s, n) for s in ("q", "win") for n in first]
                 + [(n, "merge") for n in ("hn", "gh", "v2")] + [(n, "plan") for n in ("gh_old", "v2_old", "as_text")] + [("as_search", "as_text")]
                 + [("plan", "follow"), ("follow", "merge")]
                 + [("merge", f"cls_{s}") for s in ("hn", "gh", "v2", "as")]
                 + [(f"cls_{s}", "verify") for s in ("hn", "gh", "v2", "as")]
                 + [("verify", "llm_cluster"), ("llm_cluster", "fin"), ("fin", "comp"), ("comp", "report"),
                    ("report", "end")]):
        g.link(a, b)
    g.link("followstart", "follow_http", iteration="follow")

    return {
        "app": {"name": "需求雷达", "description": "领域 → 抓 HN / GitHub / V2EX 讨论 + App Store 差评（近期为主）→ 需求判定（原话核对）→ 痛点聚类打分 → 竞品调研（内核 MCP）",
                "icon": "📡", "icon_background": "#E0F2FE", "mode": "workflow", "use_icon_as_answer_icon": False},
        "kind": "app", "version": "0.7.0", "dependencies": [],
        "workflow": {
            "conversation_variables": [], "environment_variables": [],
            "features": {"file_upload": {"enabled": False}, "opening_statement": "",
                         "retriever_resource": {"enabled": False}, "sensitive_word_avoidance": {"enabled": False},
                         "speech_to_text": {"enabled": False}, "suggested_questions": [],
                         "suggested_questions_after_answer": {"enabled": False}, "text_to_speech": {"enabled": False}},
            "graph": {"nodes": g.nodes, "edges": g.edges, "viewport": {"x": 0, "y": 0, "zoom": 0.5}}}}


class _Dumper(yaml.SafeDumper):
    pass


_Dumper.add_representer(str, lambda d, s: d.represent_scalar("tag:yaml.org,2002:str", s, style="|" if "\n" in s else None))

if __name__ == "__main__":
    out = Path(__file__).parent / "dist" / "demand_radar.yml"
    doc = build()
    out.write_text(yaml.dump(doc, Dumper=_Dumper, allow_unicode=True, sort_keys=False, width=10000), encoding="utf-8")
    print("wrote", out, len(doc["workflow"]["graph"]["nodes"]), "nodes")
