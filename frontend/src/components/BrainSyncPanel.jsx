import { useEffect, useRef, useState } from "react";
import { Brain, Download, Upload } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";

const GROUP_META = {
  config: { label: "Config + rules", hint: "bot config (minus live/feed toggles), classifier rules, saved defaults" },
  doctor: { label: "Doctor memory", hint: "suggestions & applied history, autopilot, breaker, canary, live-doctor, blacklist" },
  creators: { label: "Creator intel", hint: "creator history + greylist scores, wallet links, wallet graph" },
  trades: { label: "Trade history", hint: "closed trades (open positions are never imported)" },
  ticks: { label: "Tick store (48h)", hint: "replay / gate-ledger evidence — expires in 48h, large" },
};
const CHUNK = 2 * 1024 * 1024;

const fmtN = (n) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n));

export default function BrainSyncPanel({ onApplied }) {
  const [summary, setSummary] = useState(null);
  const [groups, setGroups] = useState(["config", "doctor", "creators", "trades"]);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState(null);
  const [result, setResult] = useState(null);
  const fileInput = useRef(null);

  useEffect(() => { api.brainSummary().then(setSummary).catch(() => {}); }, []);

  const toggle = (g) => setGroups((cur) => (cur.includes(g) ? cur.filter((x) => x !== g) : [...cur, g]));

  const handleExport = async () => {
    if (!groups.length) return toast.error("Pick at least one group");
    setBusy(true);
    setProgress("exporting…");
    try {
      const res = await fetch(api.brainExportUrl(groups), { credentials: "include" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const name = (res.headers.get("content-disposition") || "").match(/filename="([^"]+)"/)?.[1] || "bot-brain.ndjson.gz";
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = name; a.click();
      URL.revokeObjectURL(url);
      toast.success(`Brain exported · ${(blob.size / 1048576).toFixed(1)} MB`);
    } catch (e) {
      toast.error(`Export failed: ${e?.message || e}`);
    } finally {
      setBusy(false);
      setProgress(null);
    }
  };

  const handleImport = async (event) => {
    const file = event.target.files?.[0];
    if (fileInput.current) fileInput.current.value = "";
    if (!file) return;
    if (!groups.length) return toast.error("Pick at least one group to import");
    if (!window.confirm(`Import "${file.name}" (${(file.size / 1048576).toFixed(1)} MB)?\n\nMerge policy: newer copy wins, open positions and live/feed toggles are never touched. The bot is auto-paused — press Start again after review.`)) return;
    setBusy(true);
    setResult(null);
    try {
      const { upload_id } = await api.brainImportBegin(file.name, file.size);
      const total = Math.ceil(file.size / CHUNK);
      for (let i = 0; i < total; i++) {
        const buf = await file.slice(i * CHUNK, (i + 1) * CHUNK).arrayBuffer();
        await api.brainImportChunk(upload_id, i, buf);
        setProgress(`uploading ${Math.round(((i + 1) / total) * 100)}%`);
      }
      await api.brainImportCommit(upload_id, groups);
      let st;
      do {
        await new Promise((r) => setTimeout(r, 1000));
        st = await api.brainImportStatus(upload_id);
        setProgress(`merging · ${fmtN(st.processed || 0)} docs`);
      } while (st.state === "running");
      if (st.state !== "done") throw new Error(st.error || "import failed");
      setResult(st);
      api.brainSummary().then(setSummary).catch(() => {});
      api.config().then((c) => onApplied?.(c)).catch(() => {});
      toast.success("Brain imported · bot paused for review");
    } catch (e) {
      toast.error(`Import failed: ${e?.response?.data?.detail || e?.message || e}`);
    } finally {
      setBusy(false);
      setProgress(null);
    }
  };

  const btn = "flex-1 flex items-center justify-center gap-1.5 px-2 py-1.5 border font-mono text-[10px] uppercase tracking-[0.15em] transition-colors duration-100 disabled:opacity-40 disabled:cursor-not-allowed border-neutral-700 text-neutral-300 hover:bg-neutral-900";

  return (
    <div data-testid="brain-sync-panel" className="mt-1 pt-3 border-t border-neutral-800/70 flex flex-col gap-2">
      <div className="text-[10px] uppercase tracking-[0.2em] text-neutral-500 flex items-center gap-2">
        <Brain className="w-3 h-3" />
        Brain Sync · learning across environments
      </div>
      <div className="grid grid-cols-1 gap-1">
        {Object.entries(GROUP_META).map(([g, m]) => (
          <label key={g} className="flex items-center justify-between gap-2 text-[10px] font-mono text-neutral-300 cursor-pointer" title={m.hint}>
            <span className="flex items-center gap-1.5 min-w-0">
              <input type="checkbox" data-testid={`brain-group-${g}`} checked={groups.includes(g)} onChange={() => toggle(g)} disabled={busy} />
              <span className="uppercase tracking-[0.1em] truncate">{m.label}</span>
            </span>
            <span className="text-neutral-500" data-testid={`brain-count-${g}`}>{summary ? `${fmtN(summary.groups?.[g]?.total ?? 0)} docs` : "…"}</span>
          </label>
        ))}
      </div>
      <div className="flex gap-2">
        <button data-testid="brain-export-btn" onClick={handleExport} disabled={busy} className={btn}>
          <Download className="w-3 h-3" /> Export brain
        </button>
        <button data-testid="brain-import-btn" onClick={() => fileInput.current?.click()} disabled={busy} className={btn}>
          <Upload className="w-3 h-3" /> Import brain
        </button>
        <input ref={fileInput} type="file" accept=".gz,application/gzip" onChange={handleImport} className="hidden" data-testid="brain-import-file-input" />
      </div>
      {progress && <div className="text-[10px] font-mono text-amber-300" data-testid="brain-progress">{progress}</div>}
      {result && (
        <div className="text-[9px] font-mono text-neutral-400 leading-snug" data-testid="brain-import-result">
          from {result.source_env || "?"} · {Object.entries(result.stats || {}).map(([c, s]) => `${c}: +${s.added} ↑${s.updated} =${s.kept}${s.skipped ? ` ✕${s.skipped}` : ""}`).join(" · ")}
        </div>
      )}
      <p className="text-[9px] font-mono text-neutral-600 leading-snug px-0.5">
        Preview and Published use separate databases — a republish moves code, not learning. Export here, import there. Merge keeps the newer copy; open positions, live toggles, feed switches and wallets stay local.
      </p>
    </div>
  );
}
