import { useState } from "react";
import { postSSE, upload, type ScreenResult } from "./api";
import Thumbs from "./Thumbs";

interface JdInput { id: string; text: string; ocr?: boolean }
interface Progress { step: string; title?: string; company?: string | null }

const STEPS = ["解析 JD", "匹配打分", "风险检查", "完成"];
let seq = 0;
const newJd = (): JdInput => ({ id: String(++seq), text: "" });

export default function ScreenPanel({ resume, onPrep }: { resume: string; onPrep: (jd: string) => void }) {
  const [jds, setJds] = useState<JdInput[]>([newJd()]);
  const [progress, setProgress] = useState<Record<string, Progress>>({});
  const [results, setResults] = useState<ScreenResult[] | null>(null);
  const [session, setSession] = useState("");
  const [running, setRunning] = useState(false);
  const [err, setErr] = useState("");

  const update = (id: string, patch: Partial<JdInput>) =>
    setJds((xs) => xs.map((x) => (x.id === id ? { ...x, ...patch } : x)));

  async function ocr(id: string, files: FileList | null) {
    if (!files?.length) return;
    update(id, { ocr: true }); setErr("");
    try { update(id, { text: await upload("/api/jd/ocr", Array.from(files), "files") }); }
    catch (e) { setErr(`截图识别失败：${(e as Error).message}`); }
    finally { update(id, { ocr: false }); }
  }

  async function run() {
    const ready = jds.filter((j) => j.text.trim());
    setRunning(true); setErr(""); setResults(null);
    setProgress(Object.fromEntries(ready.map((j) => [j.id, { step: "排队中" }])));
    try {
      await postSSE("/api/screen", { resume_text: resume, jds: ready.map(({ id, text }) => ({ id, text })) },
        (event, data) => {
          if (event === "progress")
            setProgress((p) => ({ ...p, [data.id]: { ...p[data.id], ...data } }));
          else if (event === "result") { setResults(data.ranked); setSession(data.session); }
          else if (event === "error") setErr(data.message);
        });
    } catch (e) { setErr((e as Error).message); }
    finally { setRunning(false); }
  }

  const textOf = (id: string) => jds.find((j) => j.id === id)?.text || "";
  const n = jds.filter((j) => j.text.trim()).length;

  return (
    <>
      <section className="card">
        <h2>岗位筛选</h2>
        <p className="muted">贴 1～10 个 JD（文字或截图），并行打分、标风险，给出"直接投 / 改简历后投 / 放弃"的建议。</p>
        {jds.map((j, i) => (
          <div key={j.id} className="jd-input">
            <div className="row">
              <strong>JD {i + 1}</strong>
              <label className="button small ghost">
                {j.ocr ? "识别中…" : "上传截图"}
                <input type="file" accept="image/*" multiple hidden onChange={(e) => ocr(j.id, e.target.files)} />
              </label>
              {jds.length > 1 && <button className="small ghost" onClick={() => setJds(jds.filter((x) => x.id !== j.id))}>删除</button>}
            </div>
            <textarea rows={5} value={j.text} placeholder="粘贴 JD 全文；或上传同一个岗位的 1～5 张截图"
                      onChange={(e) => update(j.id, { text: e.target.value })} />
          </div>
        ))}
        <div className="row end">
          <button className="ghost" disabled={jds.length >= 10} onClick={() => setJds([...jds, newJd()])}>+ 再加一个 JD</button>
          <button disabled={running || n === 0} onClick={run}>{running ? "筛选中…" : `开始筛选（${n} 个）`}</button>
        </div>
        {err && <p className="error">{err}</p>}
      </section>

      {Object.keys(progress).length > 0 && !results && (
        <section className="card">
          <h3>进度</h3>
          {Object.entries(progress).map(([id, p]) => (
            <div key={id} className="progress-row">
              <span className="pr-title">{p.title ? `${p.title}（${p.company || "公司未公开"}）` : `JD ${jds.findIndex((j) => j.id === id) + 1}`}</span>
              <span className="steps">
                {STEPS.map((s) => (
                  <span key={s} className={STEPS.indexOf(p.step) >= STEPS.indexOf(s) ? "dot on" : "dot"}>{s}</span>
                ))}
                {p.step === "失败" && <span className="dot bad">失败</span>}
              </span>
            </div>
          ))}
        </section>
      )}

      {results && results.map((r, i) => <ResultCard key={r.id} r={r} session={session} idx={i} onPrep={() => onPrep(textOf(r.id))} />)}
    </>
  );
}

function ResultCard({ r, session, idx, onPrep }: { r: ScreenResult; session: string; idx: number; onPrep: () => void }) {
  const [open, setOpen] = useState(false);
  if (r.error) return <section className="card"><p className="error">这个 JD 处理失败：{r.error}</p></section>;
  const cls = { 直接投: "good", 改简历后投: "mid", 放弃: "bad" }[r.advice!];
  return (
    <section className="card result">
      <div className="row between">
        <div>
          <h3>{r.title} <span className="muted">· {r.company || "公司未公开"}{r.city ? ` · ${r.city}` : ""}{r.salary ? ` · ${r.salary}` : ""}</span></h3>
          <div className="muted">{r.advice_reason}</div>
        </div>
        <div className="score">
          <span className={`badge ${cls}`}>{r.advice}</span>
          <div className="bar"><div style={{ width: `${(r.score ?? 0) * 100}%` }} /></div>
          <span>{Math.round((r.score ?? 0) * 100)}%</span>
        </div>
      </div>
      <div className="chips">
        {r.risks!.map((k, i) => (
          <span key={i} className={["外包驻场", "名不副实"].includes(k.type) ? "chip bad" : "chip"} title={k.source}>
            {k.type}：{k.detail}
            {k.source.startsWith("http") && <> <a href={k.source} target="_blank" rel="noreferrer">来源</a></>}
          </span>
        ))}
        {r.risks!.length === 0 && <span className="muted">没发现风险</span>}
      </div>
      <div className="row">
        <button className="small ghost" onClick={() => setOpen(!open)}>
          {open ? "收起" : `逐条看要求（命中 ${r.counts!["命中"]} / 部分 ${r.counts!["部分"]} / 缺失 ${r.counts!["缺失"]}）`}
        </button>
        <button className="small" onClick={onPrep}>深度准备 →</button>
        <span className="muted">建议准不准？</span><Thumbs session={session} kind="screen" idx={idx} />
      </div>
      {open && (
        <table>
          <thead><tr><th>要求</th><th>判断</th><th>理由与依据</th></tr></thead>
          <tbody>
            {r.matches!.map((m) => (
              <tr key={m.req_id}>
                <td>{m.requirement}</td>
                <td><span className={`status s-${m.status}`}>{m.status}</span></td>
                <td>{m.reason}{m.evidence.map((q, i) => <div key={i} className="quote">简历 L{q.lines.join(",")}：{q.text}</div>)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
