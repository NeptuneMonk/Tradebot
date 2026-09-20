import { memo, useEffect, useRef, useState } from "react";
import { Gauge, ChevronDown, ChevronUp } from "lucide-react";
import { api } from "@/lib/api";

const Cell = ({ label, value, warn, testId }) => (
  <div className="flex flex-col min-w-[84px]" data-testid={testId}>
    <span className="text-[9px] uppercase tracking-[0.2em] text-neutral-600">{label}</span>
    <span className={`font-mono text-xs ${warn ? "text-amber-300" : "text-neutral-200"}`}>{value}</span>
  </div>
);

/** Operator-only diagnostics row (P4.1): client WS rate + backend hub / loop stats. Collapsed by default. */
function DiagStrip({ statsRef, connected }) {
  const [open, setOpen] = useState(() => localStorage.getItem("diag_open") === "1");
  const [client, setClient] = useState({ mps: 0, avg: 0 });
  const [srv, setSrv] = useState(null);
  const lastRef = useRef({ msgs: 0, bytes: 0, t: Date.now() });

  useEffect(() => {
    if (!open) return undefined;
    const tick = () => {
      const s = statsRef.current, l = lastRef.current, now = Date.now();
      const dm = s.msgs - l.msgs, db = s.bytes - l.bytes, dt = Math.max(0.001, (now - l.t) / 1000);
      setClient({ mps: dm / dt, avg: dm ? db / dm : 0 });
      lastRef.current = { msgs: s.msgs, bytes: s.bytes, t: now };
    };
    lastRef.current = { ...statsRef.current, t: Date.now() };
    const a = setInterval(tick, 2000);
    const poll = () => api.diagnosticsLoop().then(setSrv).catch(() => {});
    poll();
    const b = setInterval(poll, 5000);
    return () => { clearInterval(a); clearInterval(b); };
  }, [open, statsRef]);

  const toggle = () => { setOpen((o) => { localStorage.setItem("diag_open", o ? "0" : "1"); return !o; }); };
  const hub = srv?.ws_hub, lag = srv?.event_loop_lag_ms;
  const kb = (n) => (n >= 1024 ? `${(n / 1024).toFixed(1)}k` : `${Math.round(n)}b`);
  return (
    <div className="border-t border-neutral-900 mt-6" data-testid="diag-strip">
      <button type="button" onClick={toggle} data-testid="diag-toggle"
        className="w-full flex items-center justify-between px-1 py-1.5 text-[9px] uppercase tracking-[0.25em] text-neutral-600 hover:text-neutral-300 transition-colors duration-100">
        <span className="flex items-center gap-1.5"><Gauge className="w-3 h-3" /> diagnostics · {connected ? "ws live" : "ws offline"}</span>
        {open ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
      </button>
      {open && (
        <div className="flex flex-wrap gap-x-6 gap-y-2 px-1 pb-3" data-testid="diag-body">
          <Cell label="client msgs/s" value={client.mps.toFixed(1)} testId="diag-client-mps" />
          <Cell label="client avg frame" value={kb(client.avg)} testId="diag-client-avg" />
          <Cell label="hub msgs/s (1m)" value={hub ? hub.msgs_per_s_1m.toFixed(1) : "—"} testId="diag-hub-mps" />
          <Cell label="hub avg frame (1m)" value={hub ? kb(hub.avg_frame_bytes_1m) : "—"} testId="diag-hub-avg" />
          <Cell label="ws clients" value={hub ? hub.clients : "—"} testId="diag-hub-clients" />
          <Cell label="hub seen / ident" value={hub ? `${hub.seen} / ${hub.ident}` : "—"} testId="diag-hub-seen" />
          <Cell label="loop lag ms" value={lag ? `${lag.last} · max ${lag.max_1m}` : "—"} warn={lag && lag.max_1m > 250} testId="diag-loop-lag" />
          <Cell label="tracked / open" value={srv ? `${srv.tracked_mints} / ${srv.active_positions}` : "—"} testId="diag-tracked" />
        </div>
      )}
    </div>
  );
}

export default memo(DiagStrip);
