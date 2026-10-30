import { useEffect, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { exportResume, postSSE, type Answer, type FinalRewrite, type ReviewItem } from "./api";
import Thumbs from "./Thumbs";

const NODES: [string, string][] = [
  ["parse", "解析 JD"], ["gap", "差距分析"], ["research", "公司调研"], ["rewrite", "简历改写 + 校验"],
  ["review", "人工确认"], ["interview", "面试题"], ["report", "生成报告"],
];

export default function PrepPanel({ resume, initialJd }: { resume: string; initialJd: string }) {
  const [jd, setJd] = useState(initialJd);
  useEffect(() => { if (initialJd) setJd(initialJd); }, [initialJd]);  // 从筛选结果点"深度准备"带过来的 JD
  const [done, setDone] = useState<string[]>([]);
  const [score, setScore] = useState<number | null>(null);
  const [thread, setThread] = useState("");
  const [review, setReview] = useState<ReviewItem[] | null>(null);
  const [report, setReport] = useState("");
  const [rewrites, setRewrites] = useState<FinalRewrite[]>([]);
  const [questions, setQuestions] = useState<{ index: number; category: string; question: string }[]>([]);
  const [exportMsg, setExportMsg] = useState("");
  const [running, setRunning] = useState(false);
  const [err, setErr] = useState("");

  function onEvent(event: string, data: any) {
    if (event === "thread") setThread(data.thread_id);
    else if (event === "node") {
      setDone((d) => [...d, data.name]);
      if (data.score !== undefined) setScore(data.score);
    } else if (event === "review") setReview(data.items);
    else if (event === "report") { setReport(data.markdown); setRewrites(data.rewrites); setQuestions(data.questions); }
    else if (event === "error") setErr(data.message);
  }

  async function call(url: string, body: unknown) {
    setRunning(true); setErr("");
    try { await postSSE(url, body, onEvent); }
    catch (e) { setErr((e as Error).message); }
    finally { setRunning(false); }
  }

  function start() {
    setDone([]); setScore(null); setReview(null); setReport(""); setThread(""); setRewrites([]); setQuestions([]); setExportMsg("");
    call("/api/prep", { resume_text: resume, jd_text: jd });
  }

  function submit(answers: Record<string, Answer>) {
    setReview(null);
    call(`/api/prep/${thread}/resume`, { resume_text: resume, answers });
  }

  async function downloadDocx() {
    setExportMsg("");
    try {
      const missed = await exportResume(resume, rewrites);
      setExportMsg(missed ? `有 ${missed} 条没能自动替换，已列在文档末尾，请手动改` : "已下载，黄色高亮处为改写内容");
    } catch (e) { setExportMsg(`导出失败：${(e as Error).message}`); }
  }

  function download() {
    const url = URL.createObjectURL(new Blob([report], { type: "text/markdown;charset=utf-8" }));
    const a = Object.assign(document.createElement("a"), { href: url, download: "面试准备.md" });
    a.click();
    URL.revokeObjectURL(url);
  }

  const started = running || done.length > 0;
  return (
    <>
      <section className="card">
        <h2>深度准备</h2>
        <p className="muted">
          拿到面试后用：差距分析、公司调研（每条附来源）、简历改写（每条可追溯原文，防编造校验没过的交给你确认）、面试题。约 1～2 分钟。
        </p>
        <textarea rows={8} value={jd} onChange={(e) => setJd(e.target.value)} placeholder="粘贴 JD 全文（或在筛选结果里点「深度准备」）" />
        <div className="row end">
          <button disabled={running || !jd.trim()} onClick={start}>{running ? "进行中…" : "开始准备"}</button>
        </div>
        {err && <p className="error">{err}</p>}
      </section>

      {started && (
        <section className="card">
          <div className="steps">
            {NODES.map(([k, label]) => (
              <span key={k} className={done.includes(k) ? "dot on" : review && k === "review" ? "dot wait" : "dot"}>
                {label}{k === "gap" && score !== null ? ` ${Math.round(score * 100)}%` : ""}
              </span>
            ))}
          </div>
        </section>
      )}

      {review && <ReviewForm items={review} onSubmit={submit} />}

      {report && rewrites.length > 0 && (
        <section className="card">
          <div className="row between">
            <h3>简历改写（{rewrites.filter((r) => r.final_text !== r.original).length} 条生效）</h3>
            <button className="small" onClick={downloadDocx}>下载改写后的简历（Word）</button>
          </div>
          {exportMsg && <p className="muted">{exportMsg}</p>}
          {rewrites.map((r) => (
            <div key={r.index} className="rewrite-item">
              <div className="body">
                {r.final_text !== r.original && <div className="old">{r.original}</div>}
                <div>{r.final_text}</div>
                <div className="muted">{r.decision}</div>
              </div>
              {r.final_text !== r.original && <Thumbs session={thread} kind="rewrite" idx={r.index} />}
            </div>
          ))}
        </section>
      )}

      {report && questions.length > 0 && (
        <section className="card">
          <h3>面试题（觉得押得准就点 👍，帮我们改进）</h3>
          {questions.map((q) => (
            <div key={q.index} className="q-item">
              <span>【{q.category}】{q.question}</span>
              <Thumbs session={thread} kind="question" idx={q.index} />
            </div>
          ))}
        </section>
      )}

      {report && (
        <section className="card report">
          <div className="row end"><button className="small" onClick={download}>下载 Markdown</button></div>
          <Markdown remarkPlugins={[remarkGfm]}
                    components={{ a: (p) => <a {...p} target="_blank" rel="noreferrer" /> }}>{report}</Markdown>
        </section>
      )}
    </>
  );
}

function ReviewForm({ items, onSubmit }: { items: ReviewItem[]; onSubmit: (a: Record<string, Answer>) => void }) {
  // 默认"拒绝"：宁可不改，也不留编造
  const [answers, setAnswers] = useState<Record<string, Answer>>(
    () => Object.fromEntries(items.map((it) => [String(it.index), { action: "reject" }])));
  const set = (i: number, a: Answer) => setAnswers((x) => ({ ...x, [String(i)]: a }));

  return (
    <section className="card review">
      <h3>有 {items.length} 条改写没通过防编造校验，请逐条确认</h3>
      <p className="muted">程序检查：数字、技术名、"主导"一类的词是否在原文里；模型复核：有没有夸大范围、程度。默认拒绝（保留原文）。</p>
      {items.map((it) => {
        const a = answers[String(it.index)];
        return (
          <div key={it.index} className="review-item">
            <div><span className="label">原文</span>{it.original}</div>
            <div><span className="label">改写</span>{it.new_text}</div>
            <ul className="problems">
              {it.rule_problems.map((p, i) => <li key={i}>程序检查：{p}</li>)}
              {it.verdict !== "通过" && <li>模型复核：{it.verdict}，{it.reason}</li>}
            </ul>
            <details><summary>依据原文（简历中的对应几行）</summary><div className="quote">{it.source}</div></details>
            <div className="row">
              {(["accept", "edit", "reject"] as const).map((act) => (
                <label key={act} className="radio">
                  <input type="radio" checked={a.action === act}
                         onChange={() => set(it.index, act === "edit" ? { action: "edit", text: it.new_text } : { action: act })} />
                  {{ accept: "采纳改写", edit: "我来改", reject: "拒绝，保留原文" }[act]}
                </label>
              ))}
            </div>
            {a.action === "edit" && (
              <textarea rows={2} value={a.text} onChange={(e) => set(it.index, { action: "edit", text: e.target.value })} />
            )}
          </div>
        );
      })}
      <div className="row end"><button onClick={() => onSubmit(answers)}>提交，继续生成</button></div>
    </section>
  );
}
