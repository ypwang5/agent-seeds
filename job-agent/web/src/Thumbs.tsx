import { useState } from "react";
import { sendFeedback, type Vote } from "./api";

/** 👍👎：再点一次同一个就是取消。 */
export default function Thumbs({ session, kind, idx }: { session: string; kind: "screen" | "rewrite" | "question"; idx: number }) {
  const [vote, setVote] = useState<Vote>(null);
  const click = (v: "up" | "down") => {
    const next = vote === v ? null : v;
    setVote(next);
    if (session) sendFeedback(session, kind, idx, next);
  };
  return (
    <span className="thumbs">
      <button className={vote === "up" ? "thumb on" : "thumb"} title="有用" onClick={() => click("up")}>👍</button>
      <button className={vote === "down" ? "thumb on" : "thumb"} title="没用" onClick={() => click("down")}>👎</button>
    </span>
  );
}
