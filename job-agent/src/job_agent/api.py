"""网页后端（FastAPI + SSE）。

    uv run uvicorn job_agent.api:app --port 8001   # 本机 8000 被 Docker（Dify）占用

为什么用 SSE（服务器推送事件）：筛选 10 个 JD、深度准备一次都要几十秒，
用 SSE 把"第几个 JD 做到哪一步"实时推给浏览器，而不是让用户盯着转圈。
SSE 就是一个不关闭的 HTTP 响应，服务器每有新进度就写一段 "event: xxx / data: {...}"。

隐私：服务器不保存简历。每次请求浏览器都把简历文本发过来，服务器脱敏后才进图；
深度准备暂停后恢复时，浏览器再发一次同一份简历，重新脱敏得到同样的占位符映射，再把报告还原。
存档（checkpoint）里只有脱敏后的内容。

费用：请求头带 X-DeepSeek-Key 用用户自己的 Key；不带就用服务器的 Key，按 IP 每天限额，另有全站每天总上限
（按 IP 限可以换 IP 绕过，总上限保证最坏情况下的花费有数）。截图识别用的是服务器的通义 Key，带不带 Key 都计入额度。

注意：流式接口一旦开始 yield，响应头（含状态码）就已经发出去了，再抛 HTTPException 前端只会看到连接断开。
所以参数检查、限额都放在依赖（Depends）里——FastAPI 先跑完依赖，再开始流式输出。
"""
import asyncio
import logging
import os
import time
import queue
import sqlite3
import threading
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator, Callable
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, Response, UploadFile
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from pydantic import BaseModel

from job_agent.core.llm import USER_KEY
from job_agent.core.pii import Masker
from job_agent.core.tracing import enabled as tracing_enabled, tracing_config
from job_agent.feedback import FeedbackStore, Kind, Vote
from job_agent.job import prep_graph, screen_graph
from job_agent.job.export import apply_rewrites, to_docx
from job_agent.job.ocr import ocr_jd
from job_agent.job.parse import number_lines, read_text

ROOT = Path(__file__).parents[2]
DATA = ROOT / "prep_out"
FREE_PER_DAY = int(os.environ.get("JOB_FREE_PER_DAY", "20"))          # 不带 Key 时每个 IP 每天可处理的 JD 数
FREE_TOTAL_PER_DAY = int(os.environ.get("JOB_FREE_TOTAL_PER_DAY", "200"))  # 全站每天免费总量（约 200 × 3 分 = 6 元封顶）
PREP_COST = 3                                                           # 一次深度准备按 3 个 JD 计
MAX_JDS = 10
MAX_RESUME_CHARS, MAX_JD_CHARS = 20_000, 10_000   # 防止一次塞几十万字（按 token 计费）
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
KEEP_DAYS = 7                                      # 深度准备的存档保留天数

app = FastAPI(title="求职 Agent")
log = logging.getLogger("uvicorn.error")   # 跟 uvicorn 的日志走同一个输出，线上用 journalctl 能看到
DATA.mkdir(exist_ok=True)
# check_same_thread=False：图在后台线程里跑；SqliteSaver 内部自带锁，多线程共用一个连接是安全的
_saver = SqliteSaver(sqlite3.connect(DATA / "web_checkpoints.sqlite", check_same_thread=False))
PREP = prep_graph.build_graph(_saver)
SCREEN = screen_graph.build_graph()
FEEDBACK = FeedbackStore(DATA / "feedback.sqlite")   # 只存计数，不存文字，不随存档清理
_usage: dict[tuple[str, str], int] = defaultdict(int)
_usage_lock = threading.Lock()


# ---------- 公共 ----------

def client_ip(request: Request) -> str:
    """线上经 Cloudflare Tunnel 转发，所有请求的来源都是本机 127.0.0.1，真实 IP 在 CF-Connecting-IP 头里。
    只在请求确实来自本机时才信这个头：直连的请求可以伪造任意请求头。"""
    host = request.client.host if request.client else "?"
    if host in ("127.0.0.1", "::1"):
        return request.headers.get("cf-connecting-ip", host)
    return host


def charge(request: Request, user_key: str | None, cost: int):
    """没带 Key 的记账，超额返回 429。进程重启会清零（第一版够用）。"""
    if user_key:
        return
    today = date.today().isoformat()
    k, total = (client_ip(request), today), ("*", today)
    with _usage_lock:
        if _usage[total] + cost > FREE_TOTAL_PER_DAY:
            raise HTTPException(429, "今天全站的免费额度用完了，可以在设置里填自己的 DeepSeek Key，或明天再来")
        if _usage[k] + cost > FREE_PER_DAY:
            raise HTTPException(429, f"今天的免费额度（{FREE_PER_DAY} 个 JD）用完了，可以在设置里填自己的 DeepSeek Key")
        _usage[k] += cost
        _usage[total] += cost


_last_cleanup = 0.0


def cleanup_checkpoints(now: float | None = None):
    """删掉 KEEP_DAYS 天前开始的深度准备任务（存档只增不删会占满硬盘）。最多每小时做一次。"""
    global _last_cleanup
    now = now or time.time()
    if now - _last_cleanup < 3600:
        return
    _last_cleanup = now
    started: dict[str, str] = {}  # 任务编号 → 最早一次存档的时间（ISO 字符串可以直接比大小）
    for c in _saver.list(None):
        t = c.config["configurable"]["thread_id"]
        started[t] = min(started.get(t, c.checkpoint["ts"]), c.checkpoint["ts"])
    cutoff = datetime.fromtimestamp(now - KEEP_DAYS * 86400, timezone.utc).isoformat()
    for thread, ts in started.items():
        if ts < cutoff:
            _saver.delete_thread(thread)


async def run_streaming(work: Callable[[Callable[[str, Any], None]], None], user_key: str | None
                        ) -> AsyncIterator[ServerSentEvent]:
    """在后台线程里跑同步的图，事件经队列转成 SSE。

    为什么不直接在接口里跑：图是同步代码，会卡住整个服务的事件循环；放进线程后，
    还能在线程开头设置 USER_KEY，图里所有节点（包括 LangGraph 并行开的任务）都继承这个 Key。
    """
    q: queue.Queue = queue.Queue()

    def target():
        USER_KEY.set(user_key)
        try:
            work(lambda event, data: q.put(ServerSentEvent(event=event, data=data)))
        except Exception as e:  # 出错也要告诉前端，否则前端一直等；同时写服务器日志，否则线上出错无从查起
            log.exception("流式任务出错")
            q.put(ServerSentEvent(event="error", data={"message": f"{type(e).__name__}: {str(e)[:300]}"}))
        finally:
            q.put(None)

    threading.Thread(target=target, daemon=True).start()
    while (item := await asyncio.to_thread(q.get)) is not None:
        yield item


def masked_lines(resume_text: str) -> tuple[list[str], Masker]:
    masker = Masker()
    return number_lines(masker.mask(resume_text)), masker


def unmask_obj(obj: Any, masker: Masker) -> Any:
    """把返回给浏览器的数据里的占位符还原（只在响应里还原，存档里仍是占位符）。"""
    if isinstance(obj, str):
        return masker.unmask(obj)
    if isinstance(obj, list):
        return [unmask_obj(x, masker) for x in obj]
    if isinstance(obj, dict):
        return {k: unmask_obj(v, masker) for k, v in obj.items()}
    return obj


# ---------- 文件转文本 ----------

@app.get("/api/health")
def health():
    return {"ok": True, "free_per_day": FREE_PER_DAY, "tracing": tracing_enabled()}


@app.post("/api/resume/text")
async def resume_text(file: UploadFile = File(...)):
    """简历文件 → 文本。临时文件用完即删，不保存。"""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in (".pdf", ".txt", ".md"):
        raise HTTPException(400, "只支持 PDF / txt / md")
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "文件超过 5MB")
    if suffix != ".pdf":
        return {"text": data.decode("utf-8", errors="ignore")}
    tmp = DATA / f"tmp_{uuid.uuid4().hex}.pdf"
    try:
        tmp.write_bytes(data)
        return {"text": await asyncio.to_thread(read_text, tmp)}
    finally:
        tmp.unlink(missing_ok=True)


@app.post("/api/jd/ocr")
async def jd_ocr(request: Request, files: list[UploadFile] = File(...)):
    """同一个 JD 的 1～5 张截图 → JD 文本（用服务器的通义 Key，计入免费额度）。"""
    if not 1 <= len(files) <= 5:
        raise HTTPException(400, "一次上传 1～5 张截图")
    charge(request, None, 1)  # 截图识别用的是服务器的通义 Key，带了 DeepSeek Key 也照样计入额度
    images = [(await f.read(MAX_UPLOAD_BYTES + 1), f.content_type or "image/png") for f in files]
    if any(len(d) > MAX_UPLOAD_BYTES for d, _ in images) or any(not m.startswith("image/") for _, m in images):
        raise HTTPException(400, "只支持图片，每张不超过 5MB")
    return {"text": await asyncio.to_thread(ocr_jd, images)}


# ---------- 岗位筛选 ----------

class JD(BaseModel):
    id: str
    text: str


class ScreenReq(BaseModel):
    resume_text: str
    jds: list[JD]


def check_lengths(resume_text: str, jd_texts: list[str]):
    if len(resume_text) > MAX_RESUME_CHARS:
        raise HTTPException(400, f"简历太长（超过 {MAX_RESUME_CHARS} 字）")
    if any(len(t) > MAX_JD_CHARS for t in jd_texts):
        raise HTTPException(400, f"JD 太长（超过 {MAX_JD_CHARS} 字）")


def screen_checked(req: ScreenReq, request: Request, x_deepseek_key: str | None = Header(None)):
    if not 1 <= len(req.jds) <= MAX_JDS:
        raise HTTPException(400, f"一次 1～{MAX_JDS} 个 JD")
    check_lengths(req.resume_text, [jd.text for jd in req.jds])
    charge(request, x_deepseek_key, len(req.jds))
    return req, x_deepseek_key


@app.post("/api/screen", response_class=EventSourceResponse)
async def screen(checked=Depends(screen_checked)):
    """事件：progress {id, step, title?, company?} …… → result {ranked, session}。"""
    req, x_deepseek_key = checked
    lines, masker = masked_lines(req.resume_text)
    session = uuid.uuid4().hex

    def work(send):
        config = {"max_concurrency": 4, **tracing_config(session, "岗位筛选")}
        inp = {"lines": lines, "jds": [jd.model_dump() for jd in req.jds]}
        for mode, chunk in SCREEN.stream(inp, config, stream_mode=["custom", "values"]):
            if mode == "custom":
                send("progress", chunk)
            elif "ranked" in chunk:
                # session：前端点 👍👎 时带上
                send("result", {"ranked": unmask_obj(chunk["ranked"], masker), "session": session})

    async for ev in run_streaming(work, x_deepseek_key):
        yield ev


# ---------- 深度准备 ----------

class PrepReq(BaseModel):
    resume_text: str
    jd_text: str


class ResumeReq(BaseModel):
    resume_text: str
    answers: dict[str, dict]   # {"改写编号": {"action": "accept|edit|reject", "text": "..."}}


def prep_work(inp: Any, thread: str, masker: Masker):
    """事件：thread {thread_id} → node {name, score?} …… → review {items}（暂停）或 report {markdown, rewrites, questions}。"""
    def work(send):
        config = {"configurable": {"thread_id": thread}, **tracing_config(thread, "深度准备")}
        send("thread", {"thread_id": thread})
        for chunk in PREP.stream(inp, config, stream_mode="updates"):
            for node, update in chunk.items():
                if node == "__interrupt__":
                    continue
                extra = {"score": round(prep_graph._gap(update).score, 3)} if node == "gap" else {}
                send("node", {"name": node, **extra})
        state = PREP.get_state(config)
        if state.next:  # 停在人工确认
            send("review", unmask_obj(state.tasks[0].interrupts[0].value, masker))
        else:
            values = state.values
            FEEDBACK.record_run(thread, values.get("rewrites", []))
            rewrites = [{"index": i, "lines": r["item"]["lines"], "original": r["item"]["original"],
                         "final_text": r.get("final_text", ""), "decision": r.get("decision", "")}
                        for i, r in enumerate(values.get("rewrites", []))]
            questions = [{"index": i, "category": q["category"], "question": q["question"]}
                         for i, q in enumerate(values.get("questions", []))]
            send("report", unmask_obj({"markdown": values["report_md"], "rewrites": rewrites,
                                       "questions": questions}, masker))
    return work


def prep_checked(req: PrepReq, request: Request, x_deepseek_key: str | None = Header(None)):
    check_lengths(req.resume_text, [req.jd_text])
    charge(request, x_deepseek_key, PREP_COST)
    cleanup_checkpoints()
    return req, x_deepseek_key


@app.post("/api/prep", response_class=EventSourceResponse)
async def prep(checked=Depends(prep_checked)):
    req, x_deepseek_key = checked
    lines, masker = masked_lines(req.resume_text)
    thread = uuid.uuid4().hex  # 不可猜的编号：没有登录，谁拿到编号谁就能恢复这个任务
    inp = {"jd_text": req.jd_text, "lines": lines, "problems": []}
    async for ev in run_streaming(prep_work(inp, thread, masker), x_deepseek_key):
        yield ev


def resume_checked(thread: str, req: ResumeReq, x_deepseek_key: str | None = Header(None)):
    state = PREP.get_state({"configurable": {"thread_id": thread}})
    if not state.next:
        raise HTTPException(404, "任务不存在或已经结束")
    lines, masker = masked_lines(req.resume_text)
    if state.values["lines"] != lines:
        raise HTTPException(400, "简历和开始时的不一致，无法还原个人信息")
    return thread, req, masker, x_deepseek_key


@app.post("/api/prep/{thread}/resume", response_class=EventSourceResponse)
async def prep_resume(checked=Depends(resume_checked)):
    thread, req, masker, x_deepseek_key = checked
    # 人工修改的文本里可能带真实姓名等：先脱敏再存进图
    answers = {k: {**v, "text": masker.mask(v["text"])} if v.get("text") else v for k, v in req.answers.items()}
    async for ev in run_streaming(prep_work(Command(resume=answers), thread, masker), x_deepseek_key):
        yield ev


# ---------- 反馈、统计、导出 ----------

class FeedbackReq(BaseModel):
    session: str
    kind: Kind
    idx: int
    vote: Vote | None = None   # None = 取消


@app.post("/api/feedback")
def feedback(req: FeedbackReq):
    if not (len(req.session) == 32 and req.session.isalnum()) or not 0 <= req.idx < 100:
        raise HTTPException(400, "参数不对")
    FEEDBACK.vote(req.session, req.kind, req.idx, req.vote)
    return {"ok": True}


@app.get("/api/stats")
def stats():
    """汇总数字（不含任何内容），用来看产品指标。"""
    return FEEDBACK.stats()


class ExportReq(BaseModel):
    resume_text: str
    rewrites: list[dict]   # [{lines, original, final_text}]，来自 report 事件


@app.post("/api/export/resume")
def export_resume(req: ExportReq):
    """把改写套回简历全文，返回 Word。只在内存里生成，不保存。"""
    check_lengths(req.resume_text, [])
    if len(req.rewrites) > 20:
        raise HTTPException(400, "改写条数过多")
    paragraphs, missed = apply_rewrites(req.resume_text, req.rewrites)
    return Response(to_docx(paragraphs, missed),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    headers={"Content-Disposition": "attachment; filename*=UTF-8''resume.docx",
                             "X-Missed": str(len(missed))})


# 生产环境：前端打包产物由同一个服务提供（开发时用 Vite 自己的服务器 + 代理）
_dist = ROOT / "web" / "dist"
if _dist.exists():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="web")
