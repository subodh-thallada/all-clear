import { useEffect, useRef } from "react";
import type { FeedLine, Party } from "../api/types";
import { effectBadge, isGate, isRefusal, splitRule } from "./format";
import { useUi } from "./store";

function GateText({ text }: { text: string }) {
  const parts = splitRule(text);
  if (!parts) return <>{text}</>;
  return (
    <>
      {parts[0]}
      <mark>{parts[1]}</mark>
      {parts[2]}
    </>
  );
}

function Line({ line, party, fresh }: { line: FeedLine; party: Party | undefined; fresh: boolean }) {
  const selected = useUi((s) => s.selected);
  const select = useUi((s) => s.select);
  const refusal = isRefusal(line);
  const gate = !refusal && isGate(line);
  const badge = effectBadge(line);
  const sys = line.system.split(" ")[0].toLowerCase();
  return (
    <li className={`feed-line ${line.level}${fresh ? " fresh" : ""}`}>
      <div className="fl-meta">
        <span className="fl-time num">{line.sim_label}</span>
        <span className={`fl-sys ${sys}${refusal ? " refusal" : ""}`}>{line.system}</span>
        {badge === "sent" && <span className="chip sent" title="Sent: not yet confirmed">Sent</span>}
        {badge === "verified" && <span className="chip good" title="Verified: the effect is confirmed">Verified</span>}
        {party && (party.simulated ? <span className="chip sim">Simulated</span> : <span className="chip real">Real</span>)}
        {party && (
          <button type="button" className={`fl-party${selected === party.party_id ? " selected" : ""}`}
            onClick={() => select(party.party_id)} aria-label={`Inspect ${party.name}`}>
            {party.name}
          </button>
        )}
      </div>
      {refusal ? (
        <pre className="verbatim">{line.text}</pre>
      ) : gate ? (
        <pre className="verbatim gate">
          <GateText text={line.text} />
        </pre>
      ) : (
        <div className="fl-text">{line.text}</div>
      )}
    </li>
  );
}

export default function Feed({ feed, parties }: { feed: FeedLine[]; parties: Party[] }) {
  const byId = new Map(parties.map((p) => [p.party_id, p]));
  const seen = useRef<Set<string> | null>(null);
  const known = seen.current;
  useEffect(() => {
    seen.current = new Set(feed.map((l) => l.id));
  }, [feed]);
  return (
    <section className="panel glass feed" aria-labelledby="feed-title">
      <div className="panel-head">
        <h2 id="feed-title" className="panel-title">Feed</h2>
        <span className="panel-meta">newest first · Atlas refusals verbatim</span>
      </div>
      {feed.length ? (
        <ol className="feed-list" role="log" aria-live="off" aria-label="What happened, newest first">
          {feed.map((line) => (
            <Line key={line.id} line={line} party={line.party_id ? byId.get(line.party_id) : undefined}
              fresh={known !== null && !known.has(line.id)} />
          ))}
        </ol>
      ) : (
        <p className="feed-empty">Nothing yet. The feed fills as the run acts.</p>
      )}
    </section>
  );
}
