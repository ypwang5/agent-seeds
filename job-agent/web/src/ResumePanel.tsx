import { useState } from "react";
import { upload } from "./api";

export default function ResumePanel({ resume, setResume, onDone }:
  { resume: string; setResume: (s: string) => void; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  async function onFile(f: File | undefined) {
    if (!f) return;
    setBusy(true); setErr("");
    try { setResume(await upload("/api/resume/text", [f], "file")); }
    catch (e) { setErr(String((e as Error).message)); }
    finally { setBusy(false); }
  }

  return (
    <section className="card">
      <h2>我的简历</h2>
      <p className="muted">
        上传 PDF 或直接粘贴文本。简历只保存在你的浏览器里；发给模型前，服务器会把姓名、手机号、邮箱换成占位符，
        结果返回时再还原，服务器不保存简历。
      </p>
      <div className="row">
        <label className="button">
          {busy ? "读取中…" : "上传简历（PDF / txt / md）"}
          <input type="file" accept=".pdf,.txt,.md" hidden onChange={(e) => onFile(e.target.files?.[0])} />
        </label>
        {resume && <button className="ghost" onClick={() => setResume("")}>清空</button>}
      </div>
      {err && <p className="error">{err}</p>}
      <textarea rows={18} value={resume} onChange={(e) => setResume(e.target.value)}
                placeholder="在这里粘贴简历文本，可直接修改 PDF 识别有误的地方" />
      <div className="row end">
        <span className="muted">{resume.length} 字</span>
        <button disabled={!resume.trim()} onClick={onDone}>下一步：筛选岗位</button>
      </div>
    </section>
  );
}
