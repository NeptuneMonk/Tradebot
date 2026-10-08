import { useRef, useState } from "react";
import { Download, Upload, Sparkles } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";

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
      const firstLine = text.slice(0, text.indexOf("\n") > 0 ? text.indexOf("\n") : undefined);
      if (text.charCodeAt(0) === 0x1f || /\.(brain|gz|ndjson)$/i.test(file.name) || /"schema_version"/.test(firstLine)) {
        throw new Error("that is a Brain file — use “Import brain” below, not the config import");
      }
      const parsed = JSON.parse(text);
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
        const bits = [`${rep.applied.length} settings applied`, `${rep.changed.length} changed`];
        if (rep.kept_local?.length) bits.push(`${rep.kept_local.length} feed/arming switches kept local`);
        if (rep.clamped?.length) bits.push(`${rep.clamped.length} clamped: ${rep.clamped.slice(0, 4).join(", ")}`);
        toast.success(`Config imported · bot paused for review — ${bits.join(" · ")}`, { duration: 8000 });
        if (rep.ignored_unknown?.length) {
          toast.warning(`${rep.ignored_unknown.length} setting(s) in the file are unknown to this build and were ignored: ${rep.ignored_unknown.join(", ")}. Deploy the latest build, then import again.`, { duration: 15000 });
        }
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
