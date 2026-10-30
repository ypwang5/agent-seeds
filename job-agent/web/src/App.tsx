import { useEffect, useState } from "react";
import { getKey, setKey } from "./api";
import ResumePanel from "./ResumePanel";
import ScreenPanel from "./ScreenPanel";
import PrepPanel from "./PrepPanel";

type Tab = "resume" | "screen" | "prep";
const RESUME_STORE = "resume_text";

// 简历只存在浏览器本地（localStorage），每次请求随请求发给后端，后端不保存
function loadResume() { try { return localStorage.getItem(RESUME_STORE) || ""; } catch { return ""; } }

export default function App() {
  const [resume, setResume] = useState(loadResume);
  const [tab, setTab] = useState<Tab>(() => (loadResume() ? "screen" : "resume"));
  const [prepJd, setPrepJd] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [key, setKeyState] = useState(getKey);
  const [health, setHealth] = useState<{ free_per_day: number } | null>(null);

  useEffect(() => { try { localStorage.setItem(RESUME_STORE, resume); } catch { /* 隐私模式 */ } }, [resume]);
  useEffect(() => { fetch("/api/health").then((r) => r.json()).then(setHealth).catch(() => setHealth(null)); }, []);

  const tabs: [Tab, string][] = [["resume", "1 我的简历"], ["screen", "2 岗位筛选"], ["prep", "3 深度准备"]];
  return (
    <div className="app">
      <header>
        <div className="brand">求职 Agent <span className="muted">给想转 AI 应用开发的程序员</span></div>
        <nav>
          {tabs.map(([t, label]) => (
            <button key={t} className={tab === t ? "tab active" : "tab"} onClick={() => setTab(t)}
                    disabled={t !== "resume" && !resume.trim()}>{label}</button>
          ))}
          <button className="tab" onClick={() => setShowKey(!showKey)}>设置</button>
        </nav>
      </header>

      {showKey && (
        <section className="card">
          <h3>DeepSeek Key（可选）</h3>
          <p className="muted">
            不填：使用服务器的 Key，每天限 {health?.free_per_day ?? "?"} 个 JD（深度准备一次算 3 个）。
            填了：费用走你自己的账户，不受限额。Key 只保存在这个浏览器里，随请求发送，服务器不保存。
          </p>
          <div className="row">
            <input type="password" value={key} placeholder="sk-..." onChange={(e) => setKeyState(e.target.value)} />
            <button onClick={() => { setKey(key.trim()); setShowKey(false); }}>保存</button>
          </div>
        </section>
      )}

      <main>
        {/* 切换标签只隐藏不卸载：筛选结果、准备进度在来回切换时都保留 */}
        <div hidden={tab !== "resume"}><ResumePanel resume={resume} setResume={setResume} onDone={() => setTab("screen")} /></div>
        <div hidden={tab !== "screen"}><ScreenPanel resume={resume} onPrep={(jd) => { setPrepJd(jd); setTab("prep"); }} /></div>
        <div hidden={tab !== "prep"}><PrepPanel resume={resume} initialJd={prepJd} /></div>
      </main>
    </div>
  );
}
