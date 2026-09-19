import { useState } from "react";
import { ShieldCheck, ShieldAlert, ShieldQuestion, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { openWiki } from "@/lib/wikiNav";

const ICON = { pass: "✓", fail: "✗", unavailable: "?", "n/a": "–" };
const TONE = { pass: "text-emerald-300 border-emerald-900/60", fail: "text-red-300 border-red-900/60", unavailable: "text-amber-300 border-amber-900/60", "n/a": "text-neutral-500 border-neutral-800" };

export function CreatorAuditPanel({ chain, mint, initial }) {
  const [res, setRes] = useState(initial || null);
  const [busy, setBusy] = useState(false);
  const run = async (force) => {
    setBusy(true);
    try { setRes(await api.creatorAudit(chain, mint, force)); }
    catch (e) { toast.error(e?.response?.data?.detail || "audit failed"); }
    finally { setBusy(false); }
  };
  const Verdict = res?.verdict === "pass" ? ShieldCheck : res?.verdict === "skip" ? ShieldAlert : ShieldQuestion;
  return (
    <div className="border border-neutral-800 p-3 space-y-2" data-testid="creator-audit-panel">
      <div className="flex items-center justify-between gap-2">
        <div className="text-[10px] uppercase tracking-[0.2em] text-neutral-400 inline-flex items-center gap-1.5">
          <Verdict className={`w-3.5 h-3.5 ${res?.verdict === "pass" ? "text-emerald-400" : res?.verdict === "skip" ? "text-red-400" : "text-neutral-500"}`} />
          creator wallet audit
          {res && <span className={`ml-1 ${res.verdict === "pass" ? "text-emerald-300" : "text-red-300"}`} data-testid="creator-audit-verdict">{res.verdict === "pass" ? "PASS" : "WOULD SKIP"}</span>}
        </div>
        <div className="flex items-center gap-2">
          <button type="button" onClick={() => openWiki("creator-audit")} className="text-[9px] uppercase tracking-[0.15em] text-neutral-500 hover:text-neutral-200">wiki</button>
          <button type="button" onClick={() => run(!!res)} disabled={busy} data-testid="creator-audit-run"
            className="inline-flex items-center gap-1 px-2 py-0.5 border border-neutral-700 text-[9px] uppercase tracking-[0.15em] text-neutral-200 hover:bg-neutral-800 disabled:opacity-40 transition-colors duration-100">
            <RefreshCw className={`w-3 h-3 ${busy ? "animate-spin" : ""}`} /> {res ? "re-run" : "run audit"}
          </button>
        </div>
      </div>
      {!res && <div className="text-[10px] font-mono text-neutral-600">Not run yet. Works with the gate switched off — use it to test the checks on real launches before arming.</div>}
      {res?.reason && <div className="text-[10px] font-mono text-red-300/90" data-testid="creator-audit-reason">{res.reason}</div>}
      {res && (
        <ul className="grid grid-cols-1 sm:grid-cols-2 gap-1" data-testid="creator-audit-checks">
          {res.checks.filter((c) => c.key !== "_provider").map((c) => (
            <li key={c.key} className={`border px-2 py-1 text-[10px] font-mono leading-snug ${TONE[c.status] || TONE["n/a"]}`} title={c.detail} data-testid={`creator-audit-check-${c.key}`}>
              <span className="mr-1.5">{ICON[c.status] || "–"}</span>{c.label}
              <div className="text-neutral-500 truncate">{c.detail}</div>
            </li>
          ))}
        </ul>
      )}
      {res && <div className="text-[9px] font-mono text-neutral-600">policy for unavailable: <span className="text-neutral-400">{res.policy}</span> · cached 1h · creator {String(res.creator).slice(0, 10)}…
        {res.checks.some((c) => c.key === "_provider" && c.detail === "solscan") && <span className="ml-2 text-neutral-400" data-testid="creator-audit-attribution">Powered by <a className="underline hover:text-neutral-100" href={`https://solscan.io/account/${res.creator}`} target="_blank" rel="noreferrer">Solscan</a></span>}
      </div>}
    </div>
  );
}
