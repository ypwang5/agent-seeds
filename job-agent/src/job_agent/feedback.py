"""用户反馈与采纳率：衡量"有没有人觉得有用"的产品指标。

只记录"哪一类、第几条、赞还是踩、怎么处理的"，**不存任何文字**：改写、面试题里都有简历内容，
而对用户的承诺是服务器不保存简历。按任务编号（随机生成）关联，不记 IP。

两类数据：
- votes：用户主动点的 👍👎（筛选建议、单条改写、单道面试题）；同一任务同一条只保留最后一次；
- runs：每次深度准备结束时自动记一行改写处理结果（自动通过 / 人工采纳 / 修改 / 拒绝），算采纳率。
"""
import sqlite3
import threading
import time
from pathlib import Path
from typing import Literal

Kind = Literal["screen", "rewrite", "question"]
Vote = Literal["up", "down"]


class FeedbackStore:
    def __init__(self, path: Path | str):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS votes (ts REAL, session TEXT, kind TEXT, idx INTEGER, vote TEXT,
                                                  PRIMARY KEY (session, kind, idx));
                CREATE TABLE IF NOT EXISTS runs (ts REAL, session TEXT PRIMARY KEY, auto_pass INTEGER,
                                                 accepted INTEGER, edited INTEGER, rejected INTEGER);""")

    def vote(self, session: str, kind: Kind, idx: int, vote: Vote | None):
        """vote=None 表示取消。"""
        with self.lock, self.db:
            if vote is None:
                self.db.execute("DELETE FROM votes WHERE session=? AND kind=? AND idx=?", (session, kind, idx))
            else:
                self.db.execute("INSERT OR REPLACE INTO votes VALUES (?,?,?,?,?)",
                                (time.time(), session, kind, idx, vote))

    def record_run(self, session: str, rewrites: list[dict]):
        """rewrites：深度准备最终状态里的改写列表（只读 decision 字段）。"""
        decisions = [r.get("decision", "") for r in rewrites]
        counts = [sum(d == x for d in decisions) for x in ("自动通过", "人工确认采纳", "人工修改", "已拒绝，保留原文")]
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?)", (time.time(), session, *counts))

    def stats(self) -> dict:
        with self.lock:
            votes = {f"{k}_{v}": n for k, v, n in self.db.execute(
                "SELECT kind, vote, COUNT(*) FROM votes GROUP BY kind, vote")}
            runs, auto, acc, edit, rej = self.db.execute(
                "SELECT COUNT(*), TOTAL(auto_pass), TOTAL(accepted), TOTAL(edited), TOTAL(rejected) FROM runs").fetchone()
        total = auto + acc + edit + rej
        return {"votes": votes, "prep_runs": runs,
                "rewrites": {"total": int(total), "auto_pass": int(auto), "accepted": int(acc),
                             "edited": int(edit), "rejected": int(rej)},
                # 保留率 = 没被拒绝的改写比例（自动通过的用户没表态，所以这只是上限）；
                # 真正的"用户认可"看 votes 里 rewrite_up / (rewrite_up + rewrite_down)
                "kept_rate": round((auto + acc + edit) / total, 3) if total else None}
