// 和后端通信。SSE 用 fetch 读流而不是 EventSource：EventSource 只能发 GET，带不了简历这种大请求体。

export type Status = "命中" | "部分" | "缺失";
export interface Quote { text: string; lines: number[] }
export interface MatchItem { requirement: string; req_id: number; status: Status; evidence: Quote[]; reason: string }
export interface Risk { type: string; detail: string; source: string }
export interface ScreenResult {
  id: string; title?: string; company?: string | null; city?: string | null; salary?: string | null;
  score?: number; advice?: "直接投" | "改简历后投" | "放弃"; advice_reason?: string;
  counts?: Record<Status, number>; matches?: MatchItem[]; risks?: Risk[]; problems?: string[]; error?: string;
}
export interface ReviewItem {
  index: number; original: string; new_text: string; source: string;
  rule_problems: string[]; verdict: string; reason: string;
}
export type Answer = { action: "accept" | "edit" | "reject"; text?: string };

const KEY_STORE = "deepseek_key";
export const getKey = () => { try { return localStorage.getItem(KEY_STORE) || ""; } catch { return ""; } };
export const setKey = (k: string) => { try { localStorage.setItem(KEY_STORE, k); } catch { /* 隐私模式 */ } };

function headers(json = true): Record<string, string> {
  const h: Record<string, string> = json ? { "Content-Type": "application/json" } : {};
  const k = getKey();
  if (k) h["X-DeepSeek-Key"] = k;
  return h;
}

async function errorText(r: Response) {
  try { const j = await r.json(); return typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); }
  catch { return `${r.status} ${r.statusText}`; }
}

/** POST 一个 SSE 接口，每收到一个事件调用 onEvent(事件名, 数据)。 */
export async function postSSE(url: string, body: unknown, onEvent: (event: string, data: any) => void) {
  const r = await fetch(url, { method: "POST", headers: headers(), body: JSON.stringify(body) });
  if (!r.ok || !r.body) throw new Error(await errorText(r));
  const reader = r.body.pipeThrough(new TextDecoderStream()).getReader();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += value.replace(/\r\n/g, "\n");
    let i: number;
    while ((i = buf.indexOf("\n\n")) >= 0) {           // 事件之间用空行分隔
      const block = buf.slice(0, i);
      buf = buf.slice(i + 2);
      let event = "message", data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trim();
      }                                                  // 以 ":" 开头的是心跳注释，忽略
      if (data) onEvent(event, JSON.parse(data));
    }
  }
}

export async function upload(url: string, files: File[], field: string): Promise<string> {
  const form = new FormData();
  files.forEach((f) => form.append(field, f));
  const r = await fetch(url, { method: "POST", headers: headers(false), body: form });
  if (!r.ok) throw new Error(await errorText(r));
  return (await r.json()).text;
}

export type Vote = "up" | "down" | null;

/** 记录 👍👎：只发"哪一类第几条"，不发任何内容。失败不打扰用户。 */
export function sendFeedback(session: string, kind: "screen" | "rewrite" | "question", idx: number, vote: Vote) {
  fetch("/api/feedback", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session, kind, idx, vote }) }).catch(() => {});
}

export interface FinalRewrite { index: number; lines: number[]; original: string; final_text: string; decision: string }

/** 改写套回简历全文，下载 Word。返回没能自动替换的条数。 */
export async function exportResume(resume_text: string, rewrites: FinalRewrite[]): Promise<number> {
  const r = await fetch("/api/export/resume", { method: "POST", headers: headers(), body: JSON.stringify({ resume_text, rewrites }) });
  if (!r.ok) throw new Error(await errorText(r));
  const url = URL.createObjectURL(await r.blob());
  Object.assign(document.createElement("a"), { href: url, download: "改写后的简历.docx" }).click();
  URL.revokeObjectURL(url);
  return Number(r.headers.get("X-Missed") || 0);
}
