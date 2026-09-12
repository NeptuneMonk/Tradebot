import { useEffect, useState } from "react";
import { api } from "@/lib/api";

// Which pod answered, who holds the bot lease, how many replicas are alive. Served locally by each pod
// (never relayed) so the role you see is the role of the pod that took your request.
export default function PodPill() {
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
      data-testid="pod-pill"
      title={`this reply came from ${p.pod_id}\nlease holder: ${p.leader_id || "none"}${p.leader_seen_ago_s != null ? ` (heartbeat ${p.leader_seen_ago_s}s ago)` : ""}\npods alive: ${(p.pods || []).map((x) => `${x.role} ${x.id.slice(-9)} up ${Math.round(x.up_s / 60)}m`).join(", ") || "-"}\nfollowers relay every /api call to the leader through Mongo; only the leader runs feeds and trades`}>
      <span className={`w-2 h-2 rounded-full ${bad ? "bg-red-500 animate-pulse" : p.role === "leader" ? "bg-emerald-500" : "bg-neutral-500"}`}></span>
      <span>{label}</span>
    </span>
  );
}
