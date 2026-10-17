import { useRef, useState } from "react";
import { Download, Upload, Sparkles } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";

const KeyList = ({ label, keys, tone, testId }) => (
  <div data-testid={testId} className="flex flex-col gap-1">
    <div className={`text-[10px] uppercase tracking-[0.2em] ${tone}`}>{label} · {keys.length}</div>
    {keys.length ? (
      <div className="flex flex-wrap gap-1">
        {keys.map((k) => (
          <span key={k} className="px-1.5 py-0.5 border border-neutral-800 bg-neutral-900/60 font-mono text-[10px] text-neutral-300">{k}</span>
        ))}
      </div>
    ) : (
      <span className="font-mono text-[10px] text-neutral-600">none</span>
    )}
  </div>
);

function ImportReportDialog({ report, onClose }) {
  if (!report) return null;
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent data-testid="config-import-report-dialog" className="max-w-lg bg-neutral-950 border-neutral-800 text-neutral-200">
        <DialogHeader>
          <DialogTitle className="font-mono text-sm tracking-wide">Config imported · bot paused for review</DialogTitle>
          <DialogDescription className="font-mono text-[11px] text-neutral-400">
            {report.applied.length} settings applied · {report.changed.length} changed
            {report.kept_local?.length ? ` · ${report.kept_local.length} feed/arming switches kept local` : ""}.
            Press Start when you have reviewed the values.
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3 max-h-[50vh] overflow-y-auto pr-1">
          <KeyList label="Clamped (file value out of range → sanitised)" keys={report.clamped || []}
                   tone={report.clamped?.length ? "text-amber-400" : "text-neutral-500"} testId="config-import-clamped" />
          <KeyList label="Ignored (unknown to this build)" keys={report.ignored_unknown || []}
                   tone={report.ignored_unknown?.length ? "text-red-400" : "text-neutral-500"} testId="config-import-ignored" />
          <KeyList label="Changed" keys={report.changed || []} tone="text-emerald-400" testId="config-import-changed" />
        </div>
        <button data-testid="config-import-report-close" onClick={onClose}
                className="self-end px-3 py-1.5 border border-neutral-700 font-mono text-[10px] uppercase tracking-[0.15em] hover:bg-neutral-900">
          close
        </button>
      </DialogContent>
    </Dialog>
  );
}

/**
 * Sync the bot's config between preview & production environments — and
 * one-click apply the forensics-driven recommended defaults from the
 * 2026-05-24 analysis pass. The bot is auto-paused on import/apply so a
 * partial-merge can never trade.
 *
 * Three actions:
 *   Export  → downloads a JSON snapshot (works on any env)
 *   Import  → uploads that JSON to the other env
 *   Recommended → applies our 14-key tightened defaults in one click
 */
export default function ConfigSyncPanel({ onApplied }) {
  const [busy, setBusy] = useState(false);
  const [report, setReport] = useState(null);
  const fileInput = useRef(null);

  const handleExport = async () => {
    setBusy(true);
    try {
      const data = await api.configExport();
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      const ts = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
      a.download = `bot-config-${ts}.json`;
      a.click();
      URL.revokeObjectURL(url);
      toast.success("Config downloaded");
    } catch (e) {
      toast.error(`Export failed: ${e?.message || e}`);
    } finally {
      setBusy(false);
    }
  };

  const handleImport = async (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    setBusy(true);
    try {
      const text = await file.text();
      // Brain files: gzip magic byte, brain/gz/ndjson extension, or a parsed object with schema_version but no config.
      // (A minified one-line config export also contains "schema_version" — never regex the first line.)
      if (text.charCodeAt(0) === 0x1f || /\.(brain|gz|ndjson)$/i.test(file.name)) {
        throw new Error("that is a Brain file — use “Import brain” below, not the config import");
      }
      const parsed = JSON.parse(text);
      if (parsed && typeof parsed === "object" && parsed.schema_version !== undefined && !parsed.config) {
        throw new Error("that is a Brain file — use “Import brain” below, not the config import");
      }
      // Accept either { config: {...} } export shape or a raw config object
      const cfg = parsed.config && typeof parsed.config === "object" ? parsed.config : parsed;
      if (!cfg || typeof cfg !== "object") throw new Error("file is not a valid config JSON");
      if (!window.confirm(
        "Import this config? The bot will be auto-paused — you'll need to press Start again after reviewing values."
      )) {
        return;
      }
      const res = await api.configImport(cfg);
      const fresh = res?.config ?? res;
      const rep = res?.report;
      onApplied?.(fresh);
      if (rep) {
        setReport(rep);
        toast.success(`Config imported · ${rep.applied.length} applied · ${rep.clamped?.length || 0} clamped · ${rep.ignored_unknown?.length || 0} ignored`, { duration: 6000 });
      } else {
        toast.success("Config imported · bot paused for review");
      }
    } catch (e) {
      toast.error(`Import failed: ${e?.response?.data?.detail || e?.message || e}`);
    } finally {
      setBusy(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  };

  const handleApplyRecommended = async () => {
    if (!window.confirm(
      "Apply RECOMMENDED defaults?\n\n" +
      "These come from forensic analysis of your last 100+ live trades — they tighten entry gates, " +
      "widen exit slippage on panic exits, scale position size by classifier risk, and right-size " +
      "the priority fee.\n\nThe bot will be auto-paused — press Start to resume after review."
    )) return;
    setBusy(true);
    try {
      const fresh = await api.configApplyRecommended();
      onApplied?.(fresh);
      toast.success("Recommended defaults applied · bot paused");
    } catch (e) {
      toast.error(`Apply failed: ${e?.response?.data?.detail || e?.message || e}`);
    } finally {
      setBusy(false);
    }
  };

  const btnBase =
    "flex-1 flex items-center justify-center gap-1.5 px-2 py-1.5 border " +
    "font-mono text-[10px] uppercase tracking-[0.15em] transition-colors duration-100 " +
    "disabled:opacity-40 disabled:cursor-not-allowed";

  return (
    <div
      data-testid="config-sync-panel"
      className="mt-1 pt-3 border-t border-neutral-800/70 flex flex-col gap-2"
    >
      <ImportReportDialog report={report} onClose={() => setReport(null)} />
      <div className="text-[10px] uppercase tracking-[0.2em] text-neutral-500 flex items-center gap-2">
        <Sparkles className="w-3 h-3" />
        Config Sync
      </div>

      <button
        data-testid="apply-recommended-btn"
        onClick={handleApplyRecommended}
        disabled={busy}
        className={`${btnBase} border-amber-700/70 text-amber-300 bg-amber-950/40 hover:bg-amber-900/60`}
      >
        <Sparkles className="w-3 h-3" />
        Apply Recommended Defaults
      </button>

      <div className="flex gap-2">
        <button
          data-testid="config-export-btn"
          onClick={handleExport}
          disabled={busy}
          className={`${btnBase} border-neutral-700 text-neutral-300 hover:bg-neutral-900`}
        >
          <Download className="w-3 h-3" />
          Export
        </button>
        <button
          data-testid="config-import-btn"
          onClick={() => fileInput.current?.click()}
          disabled={busy}
          className={`${btnBase} border-neutral-700 text-neutral-300 hover:bg-neutral-900`}
        >
          <Upload className="w-3 h-3" />
          Import
        </button>
        <input
          ref={fileInput}
          type="file"
          accept="application/json,.json"
          onChange={handleImport}
          className="hidden"
          data-testid="config-import-file-input"
        />
      </div>

      <p className="text-[9px] font-mono text-neutral-600 leading-snug px-0.5">
        Use these to copy your config between Preview and Production after a redeploy.
        The bot is auto-paused on Import/Apply for safety.
      </p>
    </div>
  );
}
