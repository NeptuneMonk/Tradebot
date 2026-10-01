import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { podRoute } from "@/lib/podRoute";

const shortId = (id) => (id ? id.slice(-9) : "—");
const mins = (s) => `${Math.round((s || 0) / 60)}m`;

// Plain-English explanation of which backend copy this browser is talking to and how fast its clicks/feed are.
function explain(p, wsRole) {
  const n = p.pods_seen || 1;
  const rs = podRoute.stats();
  const total = rs.direct + rs.relayed;
  const lines = [];
  lines.push(n > 1
    ? `Production is running ${n} copies of the backend. Only the LEADER actually trades; the other copy is a stand-in that forwards to it.`
    : "One backend copy is running — it is the leader, nothing to route around.");
  lines.push(`Copies alive: ${(p.pods || []).map((x) => `${x.role} ${shortId(x.id)} (up ${mins(x.up_s)})`).join(" · ") || "—"}`);
  lines.push("");
  lines.push(`This page last spoke to: the ${p.role === "leader" ? "LEADER ✓ (fastest)" : "stand-in (slower — it forwards your request)"}`);
  lines.push(`Live feed (prices / trades / P-L): ${wsRole === "leader" ? "direct from the leader ✓" : wsRole === "follower" ? "mirrored copy, ~0.3 s behind — re-trying for a direct line every 30 s" : "not connected"}`);
  if (n > 1) {
    lines.push("");
    lines.push(`Clicks from this tab: ${rs.direct} went straight to the leader · ${rs.relayed} took the slow path (~0.5 s, forwarded through the database)`
      + (total ? ` → ${Math.round((rs.direct / total) * 100)}% fast` : ""));
    if (rs.bounced) lines.push(`${rs.bounced} were redirected on the way (instant, harmless)`);
    if (rs.pinned) lines.push("Right now the load balancer is pinning you to the stand-in, so clicks are on the slow path for ~30 s. They still work.");
  }
  return lines.join("\n");
}

export default function PodPill({ wsRole }) {
  const [p, setP] = useState(null);
  useEffect(() => {
    let alive = true;
    const load = () => api.pods().then((d) => alive && setP(d)).catch(() => {});
    load();
    const t = setInterval(load, 15000);
    return () => { alive = false; clearInterval(t); };
  }, []);
  if (!p) return null;
  const n = p.pods_seen || 1;
  const bad = p.two_leaders || (!p.leader_alive && n > 0);
  const label = p.two_leaders ? "two leaders!" : !p.leader_alive ? "no leader" : `pod: ${p.role} · ${n}`;
  return (
    <span className={`flex items-center gap-1.5 ${bad ? "text-red-300" : p.role === "leader" ? "text-emerald-300" : "text-neutral-400"}`}
      data-testid="pod-pill" title={explain(p, wsRole)}>
      <span className={`w-2 h-2 rounded-full ${bad ? "bg-red-500 animate-pulse" : p.role === "leader" ? "bg-emerald-500" : "bg-neutral-500"}`}></span>
      <span>{label}</span>
    </span>
  );
}
