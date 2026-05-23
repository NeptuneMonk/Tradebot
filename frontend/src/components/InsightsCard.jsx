import { useEffect, useState, useCallback } from "react";
import { Lightbulb, RefreshCw, Sparkles, Wrench, Sliders, Clock } from "lucide-react";
import { api } from "@/lib/api";

const CATEGORY_META = {
  "feature-new": {
    Icon: Sparkles,
    label: "NEW FEATURE",
    color: "text-fuchsia-300 border-fuchsia-900/60 bg-fuchsia-950/30",
    chip: "text-fuchsia-200 bg-fuchsia-900/40 border-fuchsia-700/50",
  },
  feature: {
    Icon: Wrench,
    label: "FEATURE",
    color: "text-amber-200 border-amber-900/60 bg-amber-950/30",
    chip: "text-amber-200 bg-amber-900/40 border-amber-700/50",
  },
  config: {
    Icon: Sliders,
    label: "CONFIG",
    color: "text-cyan-200 border-cyan-900/60 bg-cyan-950/30",
    chip: "text-cyan-200 bg-cyan-900/40 border-cyan-700/50",
  },
  timing: {
    Icon: Clock,
    label: "TIMING",
    color: "text-emerald-200 border-emerald-900/60 bg-emerald-950/30",
    chip: "text-emerald-200 bg-emerald-900/40 border-emerald-700/50",
  },
};

const CONFIDENCE_CHIP = {
  high: "border-emerald-700 text-emerald-300 bg-emerald-950/40",
  medium: "border-amber-700 text-amber-300 bg-amber-950/40",
  low: "border-neutral-700 text-neutral-400 bg-neutral-950/40",
  "n/a": "border-fuchsia-800 text-fuchsia-300 bg-fuchsia-950/40",
};

function InsightCard({ ins }) {
  const meta = CATEGORY_META[ins.category] || CATEGORY_META.config;
  const conf = ins.confidence || "low";
  const liftStr = ins.lift != null ? `${ins.lift.toFixed(2)}× lift` : "data-needed";
  const Icon = meta.Icon;
  return (
    <div
      className={`border ${meta.color} p-3`}
      data-testid={`insight-${ins.id}`}
    >
      <div className="flex items-center gap-2 flex-wrap mb-2">
        <span className={`flex items-center gap-1 text-[9px] font-mono uppercase tracking-[0.15em] px-1.5 py-0.5 border ${meta.chip}`}>
          <Icon className="w-3 h-3" />
          {meta.label}
        </span>
        <span className={`text-[9px] font-mono uppercase tracking-[0.15em] px-1.5 py-0.5 border ${CONFIDENCE_CHIP[conf]}`}>
          {conf} conf
        </span>
        <span className="text-[9px] font-mono text-neutral-500 ml-auto">{liftStr}</span>
      </div>
      <div className="text-sm font-medium text-neutral-100 mb-1.5">{ins.title}</div>
      <div className="text-[11px] text-neutral-400 mb-2 italic">{ins.evidence}</div>
      <div className="text-[11px] text-neutral-300 leading-snug border-l-2 border-neutral-700 pl-2">
        <span className="text-[10px] uppercase tracking-[0.15em] text-neutral-500">Suggested →</span>{" "}
        {ins.suggested_feature}
      </div>
    </div>
  );
}

export default function InsightsCard() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setData(await api.insights());
    } catch (e) { /* swallow */ }
    finally { setLoading(false); }
  }, []);

  useEffect(() => { load(); }, [load]);

  const ins = data?.insights || [];
  const dataDriven = ins.filter((i) => i.category !== "feature-new");
  const meta = ins.filter((i) => i.category === "feature-new");

  return (
    <div className="control-card" data-testid="insights-card">
      <div className="flex items-center justify-between mb-3">
        <span className="flex items-center gap-2 text-[10px] uppercase tracking-[0.2em] text-neutral-400">
          <Lightbulb className="w-3 h-3" /> Pattern Insights
        </span>
        <div className="flex items-center gap-3 text-[10px] font-mono text-neutral-500">
          {data && (
            <span>
              <span className="text-emerald-400">{data.winners}W</span>{" "}
              /{" "}
              <span className="text-red-400">{data.losers}L</span>{" "}
              <span className="text-neutral-600">/ {data.closed_trades} closed</span>
            </span>
          )}
          <button
            data-testid="insights-refresh-btn"
            onClick={load}
            disabled={loading}
            className="flex items-center gap-1 px-2 py-1 border border-neutral-800 hover:border-blue-700 hover:text-blue-300 transition-colors"
          >
            <RefreshCw className={`w-3 h-3 ${loading ? "animate-spin" : ""}`} />
            refresh
          </button>
        </div>
      </div>

      {!data ? (
        <div className="text-center py-6 text-[10px] uppercase tracking-[0.2em] text-neutral-600">
          loading…
        </div>
      ) : ins.length === 0 ? (
        <div className="text-center py-6 text-[10px] uppercase tracking-[0.2em] text-neutral-600">
          {data.message || "no patterns yet — keep trading and check back"}
        </div>
      ) : (
        <div className="space-y-3">
          {dataDriven.length > 0 && (
            <div className="space-y-2">
              <div className="text-[10px] uppercase tracking-[0.15em] text-neutral-500">
                Pattern Findings ({dataDriven.length})
              </div>
              {dataDriven.map((i) => <InsightCard key={i.id} ins={i} />)}
            </div>
          )}
          {meta.length > 0 && (
            <div className="space-y-2">
              <div className="text-[10px] uppercase tracking-[0.15em] text-neutral-500">
                Features the Bot Doesn't Have Yet ({meta.length})
              </div>
              {meta.map((i) => <InsightCard key={i.id} ins={i} />)}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
