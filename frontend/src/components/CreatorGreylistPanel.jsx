import { useEffect, useState, useCallback, useMemo } from "react";
import { Ghost, RefreshCw, ChevronDown, ChevronRight, Zap, FlaskConical, Shield, Play, Ban, TrendingDown, Zap as Bolt, Sparkles, BarChart3 } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";

// Color/icon tier mapping. The actual gating thresholds (45 / 70) live in
// `backend/creator_greylist.py::recommended_strategy()`. Keep these in sync
// if the backend thresholds shift.
const STRATEGY_META = {
  aggressive: {
    Icon: Zap,
    label: "AGGRESSIVE",
    wrap: "text-rose-200 border-rose-800/60 bg-rose-950/40",
    chip: "text-rose-200 bg-rose-900/40 border-rose-700/60",
    desc: "Pattern is loud + predictable. Live mode = larger size + tighter TP/SL.",
  },
  hybrid: {
    Icon: FlaskConical,
    label: "HYBRID",
    wrap: "text-amber-200 border-amber-800/60 bg-amber-950/40",
    chip: "text-amber-200 bg-amber-900/40 border-amber-700/60",
    desc: "Pattern is forming. Live mode = mild size bump + slightly tighter trail.",
  },
  standard: {
    Icon: Shield,
    label: "STANDARD",
    wrap: "text-neutral-300 border-neutral-800 bg-neutral-950/40",
    chip: "text-neutral-400 bg-neutral-900 border-neutral-800",
    desc: "Below 45 score — uses BotConfig defaults.",
  },
};

// Pattern badges per RUG_PATTERNS.md classification (creator_pattern.py).
const PATTERN_META = {
  slow_rug_tradeable: {
    label: "SLOW RUG",
    chip: "text-emerald-200 bg-emerald-900/40 border-emerald-700/60",
    Icon: TrendingDown,
    title: "Rugs at 18-30% from peak, low variance. Long entry window, predictable exit.",
  },
  predictable_dump_tradeable: {
    label: "DUMP",
    chip: "text-cyan-200 bg-cyan-900/40 border-cyan-700/60",
    Icon: Bolt,
    title: "Pump→dump→pump→rug at 12-18%. Enter on the second pump.",
  },
  fake_hype_tradeable: {
    label: "HYPE",
    chip: "text-fuchsia-200 bg-fuchsia-900/40 border-fuchsia-700/60",
    Icon: Sparkles,
    title: "Hype-keyword name + fast rug. Tradeable if you time the spike.",
  },
  untradeable_rug: {
    label: "DEAD-60s",
    chip: "text-neutral-400 bg-neutral-900 border-neutral-700",
    Icon: Ban,
    title: "Dominated by Dead-in-60s rugs. Blacklisted.",
  },
  unpredictable_rug: {
    label: "CHAOS",
    chip: "text-neutral-400 bg-neutral-900 border-neutral-700",
    Icon: Ban,
    title: "Rug variance > 20%. No reliable pattern. Blacklisted.",
  },
  unknown: {
    label: "UNKNOWN",
    chip: "text-neutral-500 bg-neutral-950 border-neutral-800",
    Icon: Shield,
    title: "Not enough data yet. Standard logic applies.",
  },
};

const MODE_CHIP = {
  live: "border-rose-700 text-rose-200 bg-rose-950/40",
  telemetry: "border-cyan-700 text-cyan-200 bg-cyan-950/40",
};

const short = (s) => (s ? `${s.slice(0, 4)}…${s.slice(-4)}` : "—");
const fmtUsd = (n) => {
  const v = Number(n || 0);
  if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(2)}M`;
  if (v >= 1_000) return `$${(v / 1_000).toFixed(1)}k`;
  return `$${v.toFixed(0)}`;
};
const fmtTime = (iso) => {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    const mins = Math.max(0, Math.floor((Date.now() - d.getTime()) / 60000));
    if (mins < 60) return `${mins}m ago`;
    if (mins < 1440) return `${Math.floor(mins / 60)}h ago`;
    return `${Math.floor(mins / 1440)}d ago`;
  } catch {
    return iso;
  }
};

// ---------------------------------------------------------------------------
// Expanded creator detail — recent failed mints + components + linked wallets
// ---------------------------------------------------------------------------
function CreatorDetail({ creator }) {
  const [profile, setProfile] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    api
      .creatorGreylistProfile(creator)
      .then((p) => {
        if (alive) setProfile(p);
      })
      .catch((e) => {
        if (alive) setError(e?.response?.data?.detail || "load failed");
      })
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [creator]);

  if (loading) {
    return (
      <div
        className="px-3 py-3 text-[10px] font-mono uppercase tracking-[0.15em] text-neutral-500"
        data-testid={`greylist-detail-loading-${creator}`}
      >
        loading profile…
      </div>
    );
  }
  if (error) {
    return (
      <div
        className="px-3 py-2 text-[11px] font-mono text-rose-400"
        data-testid={`greylist-detail-error-${creator}`}
      >
        {error}
      </div>
    );
  }
  if (!profile) return null;

  const c = profile.components || {};
  const linked = profile.linked_wallets || [];
  const recentFailed = profile.recent_failed_mints || [];
  const recentTrades = profile.recent_trades || [];
  const rugWin = profile.expected_rug_window_pct || {};
  const peakMc = profile.expected_peak_mc_usd || {};

  return (
    <div
      className="px-3 py-3 border-t border-neutral-800 bg-neutral-950/60 text-[11px] font-mono"
      data-testid={`greylist-detail-${creator}`}
    >
      {/* Pattern classification banner — what bucket the creator falls into
          and the evidence the classifier used. Mounted ABOVE the 3-col grid
          so the user sees the "why" before the "what". */}
      {profile.pattern && (() => {
        const pm = PATTERN_META[profile.pattern] || PATTERN_META.unknown;
        const PI = pm.Icon;
        const entry = profile.pattern_suggested_entry;
        const exit = profile.pattern_suggested_exit;
        return (
          <div
            className="mb-3 border border-neutral-800 px-3 py-2 bg-neutral-950"
            data-testid={`greylist-pattern-detail-${creator}`}
          >
            <div className="flex items-center gap-2 flex-wrap mb-1.5">
              <span
                className={`flex items-center gap-1 text-[9px] font-mono uppercase tracking-[0.15em] px-1.5 py-0.5 border ${pm.chip}`}
              >
                <PI className="w-3 h-3" />
                {pm.label}
              </span>
              <span className="text-neutral-500 text-[10px]">
                confidence {(profile.pattern_confidence || 0).toFixed(0)}%
              </span>
              {entry && (
                <span className="text-emerald-400 text-[10px] ml-auto">
                  entry {entry[0]?.toFixed?.(0)}–{entry[1]?.toFixed?.(0)}%
                </span>
              )}
              {exit && (
                <span className="text-rose-400 text-[10px]">
                  exit {exit[0]?.toFixed?.(0)}–{exit[1]?.toFixed?.(0)}%
                </span>
              )}
            </div>
            {(profile.pattern_evidence || []).length > 0 && (
              <ul className="text-[10px] text-neutral-400 space-y-0.5 list-disc list-inside">
                {(profile.pattern_evidence || []).map((e, i) => (
                  <li key={i}>{e}</li>
                ))}
              </ul>
            )}
          </div>
        );
      })()}

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
      {/* Score components */}
      <div>
        <div className="text-[9px] uppercase tracking-[0.2em] text-neutral-500 mb-2">
          Score Components
        </div>
        <div className="space-y-1.5">
          {[
            ["profitability", "Profit", "30%"],
            ["predictability", "Predict", "20%"],
            ["peak_mc", "Peak MC", "25%"],
            ["activity", "Activity", "15%"],
            ["volume", "Volume", "10%"],
          ].map(([k, label, weight]) => {
            const v = Number(c[k] || 0);
            return (
              <div key={k} className="flex items-center justify-between gap-2">
                <span className="text-neutral-400 w-16">{label}</span>
                <div className="flex-1 h-1.5 bg-neutral-800 relative">
                  <div
                    className="absolute inset-y-0 left-0 bg-cyan-700"
                    style={{ width: `${Math.min(100, Math.max(0, v))}%` }}
                  />
                </div>
                <span className="w-12 text-right text-neutral-200 tabular-nums">
                  {v.toFixed(0)}
                </span>
                <span className="w-9 text-right text-neutral-600">{weight}</span>
              </div>
            );
          })}
        </div>
        <div className="mt-3 grid grid-cols-2 gap-2 text-[10px]">
          <div>
            <span className="text-neutral-500 uppercase tracking-wider">Peak MC</span>
            <div className="text-neutral-100 mt-0.5">
              {peakMc.n_failed_with_peak >= 2
                ? `μ ${fmtUsd(peakMc.mean_peak_mc_usd)} · σ ${fmtUsd(peakMc.stddev_peak_mc_usd)}`
                : "insufficient data"}
            </div>
          </div>
          <div>
            <span className="text-neutral-500 uppercase tracking-wider">Rug Window</span>
            <div className="text-neutral-100 mt-0.5">
              {(rugWin.samples ?? 0) >= 4
                ? `${rugWin.lo?.toFixed?.(0) ?? "?"}–${rugWin.hi?.toFixed?.(0) ?? "?"}% from peak`
                : "insufficient data"}
            </div>
          </div>
        </div>
      </div>

      {/* Recent failed mints */}
      <div>
        <div className="text-[9px] uppercase tracking-[0.2em] text-neutral-500 mb-2 flex items-center gap-2">
          <span>Recent Failed Mints ({recentFailed.length})</span>
          <span
            className="text-neutral-600"
            title="Lifetime F count from creator stats / sweep-classified count with peak MC"
          >
            · F={profile.tokens_failed ?? "?"} · with-peak={profile.n_failed ?? 0}
          </span>
        </div>
        {recentFailed.length === 0 ? (
          <div className="text-neutral-600 italic text-[10px]">no failed mints recorded</div>
        ) : (
          <ul className="space-y-1 max-h-44 overflow-y-auto">
            {recentFailed.slice(0, 10).map((f) => (
              <li
                key={f.mint}
                className="flex items-center justify-between border border-neutral-800 px-2 py-1"
              >
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="text-neutral-200 font-semibold">{f.symbol || "?"}</span>
                    <span className="text-neutral-600 text-[10px]">{short(f.mint)}</span>
                  </div>
                  <div className="text-neutral-500 text-[10px]">
                    {f.fail_class || "—"} · {fmtTime(f.outcome_at)}
                  </div>
                </div>
                <div className="text-right">
                  <div className="text-amber-300">{fmtUsd(f.final_peak_mc_usd)}</div>
                  <div className="text-neutral-600 text-[10px]">peak</div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* Recent trades + linked wallets */}
      <div>
        <div className="text-[9px] uppercase tracking-[0.2em] text-neutral-500 mb-2">
          Our Trades on this Creator ({recentTrades.length})
        </div>
        {recentTrades.length === 0 ? (
          <div className="text-neutral-600 italic text-[10px]">no trades yet</div>
        ) : (
          <ul className="space-y-1 max-h-32 overflow-y-auto mb-3">
            {recentTrades.slice(0, 8).map((t, i) => (
              <li key={i} className="flex items-center justify-between border border-neutral-800 px-2 py-1">
                <span className="text-neutral-300">{t.symbol || short(t.mint)}</span>
                <span
                  className={
                    Number(t.pnl_pct || 0) >= 0 ? "text-emerald-400" : "text-rose-400"
                  }
                >
                  {Number(t.pnl_pct || 0) >= 0 ? "+" : ""}
                  {Number(t.pnl_pct || 0).toFixed(1)}%
                </span>
              </li>
            ))}
          </ul>
        )}
        <div className="text-[9px] uppercase tracking-[0.2em] text-neutral-500 mb-1">
          Linked Wallets ({linked.length})
        </div>
        {linked.length === 0 ? (
          <div className="text-neutral-600 italic text-[10px]">
            none traced (wallet_graph Phase 2.5)
          </div>
        ) : (
          <ul className="space-y-0.5 max-h-20 overflow-y-auto">
            {linked.slice(0, 6).map((w) => (
              <li key={w.wallet || w} className="text-neutral-400 text-[10px]">
                {short(w.wallet || w)}
              </li>
            ))}
          </ul>
        )}
      </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Single creator row in the top list
// ---------------------------------------------------------------------------
function GreylistRow({ row, expanded, onToggle }) {
  const meta = STRATEGY_META[row.recommended_strategy] || STRATEGY_META.standard;
  const Icon = meta.Icon;
  const pmc = row.expected_peak_mc_usd || {};
  const rw = row.expected_rug_window_pct || {};
  return (
    <li
      data-testid={`greylist-row-${row.creator}`}
      className="border border-neutral-800"
    >
      <button
        type="button"
        onClick={onToggle}
        className={`w-full text-left px-3 py-2 flex items-center justify-between gap-3 hover:bg-neutral-900/60 transition border-l-2 ${
          row.recommended_strategy === "aggressive"
            ? "border-l-rose-600"
            : row.recommended_strategy === "hybrid"
            ? "border-l-amber-600"
            : "border-l-neutral-800"
        }`}
      >
        <div className="flex items-center gap-2 min-w-0 flex-1">
          {expanded ? (
            <ChevronDown className="w-3 h-3 text-neutral-500 shrink-0" />
          ) : (
            <ChevronRight className="w-3 h-3 text-neutral-500 shrink-0" />
          )}
          <span
            className={`flex items-center gap-1 text-[9px] font-mono uppercase tracking-[0.15em] px-1.5 py-0.5 border ${meta.chip}`}
          >
            <Icon className="w-3 h-3" />
            {meta.label}
          </span>
          {row.pattern && row.pattern !== "unknown" && PATTERN_META[row.pattern] && (
            <span
              className={`flex items-center gap-1 text-[9px] font-mono uppercase tracking-[0.15em] px-1.5 py-0.5 border ${PATTERN_META[row.pattern].chip}`}
              title={PATTERN_META[row.pattern].title}
              data-testid={`greylist-pattern-${row.creator}`}
            >
              {(() => {
                const PI = PATTERN_META[row.pattern].Icon;
                return <PI className="w-3 h-3" />;
              })()}
              {PATTERN_META[row.pattern].label}
            </span>
          )}
          {row.signatures?.dominant_accel && row.signatures?.dominant_flow && (row.signatures?.signature_repeatability ?? 0) >= 50 && (
            <span
              className="text-[9px] font-mono uppercase tracking-[0.15em] px-1.5 py-0.5 border border-indigo-800/70 text-indigo-300 bg-indigo-950/30"
              title={`Behavior fingerprint — ${(row.signatures.signature_repeatability || 0).toFixed(0)}% of launches share this profile`}
              data-testid={`greylist-signature-${row.creator}`}
            >
              {row.signatures.dominant_accel}·{row.signatures.dominant_flow}
            </span>
          )}
          {row.links_evidence?.linked_to_rug_cluster && (
            <span
              className="text-[9px] font-mono uppercase tracking-[0.15em] px-1.5 py-0.5 border border-rose-800/70 text-rose-300 bg-rose-950/40"
              title={`Funded by ${row.links_evidence.rug_cluster_hits} known rug wallet${row.links_evidence.rug_cluster_hits > 1 ? 's' : ''}`}
              data-testid={`greylist-rug-cluster-${row.creator}`}
            >
              rug cluster · {row.links_evidence.rug_cluster_hits}
            </span>
          )}
          {!row.links_evidence?.linked_to_rug_cluster && (row.links_evidence?.n_hop1 || 0) >= 1 && (
            <span
              className="text-[9px] font-mono uppercase tracking-[0.15em] px-1.5 py-0.5 border border-amber-800/70 text-amber-300 bg-amber-950/30"
              title={`Wallet graph: ${row.links_evidence.n_hop1} hop-1 funder${row.links_evidence.n_hop1 > 1 ? 's' : ''} discovered`}
              data-testid={`greylist-links-${row.creator}`}
            >
              {row.links_evidence.n_hop1} link{row.links_evidence.n_hop1 > 1 ? 's' : ''}
            </span>
          )}
          <span className="text-sm font-mono font-semibold text-neutral-100 tabular-nums">
            {row.effective_score.toFixed(0)}
          </span>
          <span className="text-[10px] font-mono text-neutral-500 truncate">
            {short(row.creator)}
          </span>
        </div>
        <div className="flex items-center gap-4 text-[10px] font-mono shrink-0">
          <div className="text-right">
            <div className="text-neutral-500 uppercase tracking-wider text-[9px]">peak μ</div>
            <div className="text-amber-300 tabular-nums">
              {pmc.n_failed_with_peak >= 2 ? fmtUsd(pmc.mean_peak_mc_usd) : "—"}
            </div>
          </div>
          <div className="text-right">
            <div className="text-neutral-500 uppercase tracking-wider text-[9px]">rug</div>
            <div className="text-rose-300 tabular-nums">
              {(rw.samples ?? 0) >= 4 ? `~${rw.median_rug_pct?.toFixed?.(0) ?? "?"}%` : "—"}
            </div>
          </div>
          <div className="text-right">
            <div className="text-neutral-500 uppercase tracking-wider text-[9px]">fails</div>
            <div className="text-neutral-200 tabular-nums">{row.tokens_failed}</div>
          </div>
          <div className="text-right hidden md:block">
            <div className="text-neutral-500 uppercase tracking-wider text-[9px]">trades</div>
            <div className="text-neutral-200 tabular-nums">{row.n_trades}</div>
          </div>
          <div className="text-right hidden lg:block">
            <div className="text-neutral-500 uppercase tracking-wider text-[9px]">seen</div>
            <div className="text-neutral-400">{fmtTime(row.last_seen)}</div>
          </div>
        </div>
      </button>
      {expanded && <CreatorDetail creator={row.creator} />}
    </li>
  );
}

// ---------------------------------------------------------------------------
// Main panel
// ---------------------------------------------------------------------------
export default function CreatorGreylistPanel({ config, onConfigUpdate }) {
  const [items, setItems] = useState([]);
  const [blacklist, setBlacklist] = useState([]);
  const [analytics, setAnalytics] = useState(null);
  const [analyticsDays, setAnalyticsDays] = useState(30);
  const [analyticsMode, setAnalyticsMode] = useState("");  // "" = all
  const [showBlacklist, setShowBlacklist] = useState(false);
  const [loading, setLoading] = useState(false);
  const [sweepRunning, setSweepRunning] = useState(false);
  const [backfillRunning, setBackfillRunning] = useState(false);
  const [modeToggling, setModeToggling] = useState(false);
  const [minScore, setMinScore] = useState(30);
  const [expanded, setExpanded] = useState(null);
  // Panel-open state — kept closed by default so the 924-row list doesn't
  // load into the DOM (or trigger the 60s background poll) unless the
  // operator explicitly clicks to expand. Cuts mobile DOM cost and the
  // /api/creator-greylist + /api/creator-greylist/pattern-analytics calls
  // entirely for the common case where this panel is just background context.
  const [open, setOpen] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [g, b, a] = await Promise.all([
        api.creatorGreylist(25, minScore),
        api.creatorBlacklist(50).catch(() => ({ items: [] })),
        api
          .creatorPatternAnalytics(analyticsDays, analyticsMode || null)
          .catch(() => null),
      ]);
      setItems(g?.items || []);
      setBlacklist(b?.items || []);
      setAnalytics(a);
    } catch (e) {
      toast.error("Failed to load greylist");
    } finally {
      setLoading(false);
    }
  }, [minScore, analyticsDays, analyticsMode]);

  // Only fetch + poll while open. Closing the panel cancels the interval.
  useEffect(() => {
    if (!open) return;
    refresh();
    // Auto-refresh every 60s — greylist scores update on trade-close + failure-sweep,
    // so a slower poll is fine.
    const id = setInterval(refresh, 60000);
    return () => clearInterval(id);
  }, [refresh, open]);

  const runSweep = async () => {
    setSweepRunning(true);
    let toastId;
    try {
      const queued = await api.creatorGreylistRunSweep();
      toastId = toast.loading("Failure sweep queued…", { duration: Infinity });
      const job = await api.awaitJob(queued.job_id, {
        onProgress: (j) => toast.loading(`Failure sweep ${j.status}…`, { id: toastId, duration: Infinity }),
      });
      if (job.status === "error") {
        toast.error(`Sweep failed: ${job.error || "unknown"}`, { id: toastId, duration: 5000 });
      } else {
        const r = job.result || {};
        toast.success(
          `Sweep done: classified ${r?.classified ?? 0} dormant launches, ` +
            `refreshed ${r?.creators_touched ?? r?.creators_refreshed ?? 0} creators`,
          { id: toastId, duration: 5000 }
        );
        refresh();
      }
    } catch (e) {
      toast.error("Sweep failed: " + (e?.response?.data?.detail || e.message),
                  { id: toastId, duration: 5000 });
    } finally {
      setSweepRunning(false);
    }
  };

  const runBackfill = async () => {
    setBackfillRunning(true);
    let toastId;
    try {
      const queued = await api.creatorGreylistBackfill();
      toastId = toast.loading("Backfill queued…", { duration: Infinity });
      const job = await api.awaitJob(queued.job_id, {
        onProgress: (j) => toast.loading(`Backfill ${j.status}…`, { id: toastId, duration: Infinity }),
      });
      if (job.status === "error") {
        toast.error(`Backfill failed: ${job.error || "unknown"}`, { id: toastId, duration: 5000 });
      } else {
        const r = job.result || {};
        toast.success(
          `Backfill: scanned ${r?.scanned ?? 0} · ${r?.now_active_on_greylist ?? 0} now active · ${r?.now_blacklisted ?? 0} blacklisted`,
          { id: toastId, duration: 5000 }
        );
        refresh();
      }
    } catch (e) {
      toast.error("Backfill failed: " + (e?.response?.data?.detail || e.message),
                  { id: toastId, duration: 5000 });
    } finally {
      setBackfillRunning(false);
    }
  };

  const toggleMode = async () => {
    if (modeToggling) return;
    const next = mode === "live" ? "telemetry" : "live";
    // Live mode actually changes execution — confirm before flipping
    if (next === "live") {
      const ok = window.confirm(
        "Enable LIVE greylist execution?\n\n" +
          "Every new entry from a creator scoring ≥ 45 (hybrid) or ≥ 70 (aggressive)\n" +
          "will use OVERRIDE TP/SL/trail values instead of BotConfig defaults.\n" +
          "Size multipliers up to 1.5× will be applied (capped at 2× max_trade_usd).\n\n" +
          "Recommend you let telemetry mode log predictions for 24-48h first."
      );
      if (!ok) return;
    }
    setModeToggling(true);
    try {
      const updated = await api.updateConfig({ creator_greylist_mode: next });
      toast.success(`Greylist mode → ${next}`);
      onConfigUpdate && onConfigUpdate(updated);
    } catch (e) {
      toast.error("Mode toggle failed: " + (e?.response?.data?.detail || e.message));
    } finally {
      setModeToggling(false);
    }
  };

  const enabled = !!config?.creator_greylist_enabled;
  const mode = config?.creator_greylist_mode || "telemetry";
  const tierCounts = useMemo(() => {
    const c = { aggressive: 0, hybrid: 0, standard: 0 };
    for (const r of items) {
      const k = r.recommended_strategy || "standard";
      c[k] = (c[k] || 0) + 1;
    }
    return c;
  }, [items]);

  return (
    <div className="control-card" data-testid="creator-greylist-panel">
      <div className="flex items-center justify-between mb-2 flex-wrap gap-2">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          data-testid="greylist-panel-toggle"
          className="flex items-center gap-2 text-[10px] uppercase tracking-[0.2em] text-neutral-400 hover:text-neutral-200 transition"
          title={open ? "Collapse — stops the 60s data refresh" : "Expand — loads creator list + analytics"}
        >
          {open ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
          <Ghost className="w-3.5 h-3.5" />
          Creator Greylist
          {open && items.length > 0 && (
            <span className="text-neutral-600">({items.length})</span>
          )}
          {enabled ? (
            <span
              className={`ml-1 px-1.5 py-0.5 border text-[9px] font-mono uppercase tracking-wider ${
                MODE_CHIP[mode] || MODE_CHIP.telemetry
              }`}
              title={`Greylist mode: ${mode}`}
            >
              {mode}
            </span>
          ) : (
            <span className="ml-1 px-1.5 py-0.5 border border-neutral-800 text-[9px] font-mono uppercase text-neutral-500">
              disabled
            </span>
          )}
          {!open && (
            <span className="ml-2 text-neutral-600 normal-case tracking-normal text-[10px] font-mono">
              · click to load
            </span>
          )}
        </button>
        {open && (
          <div className="flex items-center gap-2" data-testid="greylist-actions">
            <label className="text-[10px] font-mono text-neutral-500 flex items-center gap-1">
              min score
              <input
                type="number"
                min="0"
                max="100"
                step="5"
                value={minScore}
                onChange={(e) => setMinScore(Number(e.target.value))}
                className="w-12 px-1.5 py-0.5 bg-neutral-950 border border-neutral-800 text-neutral-200 font-mono text-[11px]"
                data-testid="greylist-min-score-input"
              />
            </label>
            {enabled && (
              <button
                type="button"
                onClick={toggleMode}
                disabled={modeToggling}
                data-testid="greylist-mode-toggle"
                title={
                  mode === "live"
                    ? "Click to switch back to telemetry mode (no execution overrides)"
                    : "Click to enable LIVE overrides — uses tier-based TP/SL/trail/size on entry"
                }
                className={`px-1.5 py-0.5 border text-[9px] font-mono uppercase tracking-wider transition hover:brightness-125 disabled:opacity-50 ${
                  MODE_CHIP[mode] || MODE_CHIP.telemetry
                }`}
              >
                {modeToggling ? "…" : `set ${mode === "live" ? "telem" : "live"}`}
              </button>
            )}
            <button
              type="button"
              onClick={runBackfill}
              disabled={backfillRunning}
              data-testid="greylist-backfill-btn"
              className="flex items-center gap-1 px-2 py-1 border border-neutral-800 hover:bg-neutral-800 disabled:opacity-50 text-[10px] font-mono uppercase tracking-wider text-neutral-300"
              title="Re-score every creator already in DB whose tokens_failed is inside the F-band. Cheap — Mongo-only, no Helius calls."
            >
              <RefreshCw className={`w-3 h-3 ${backfillRunning ? "animate-spin" : ""}`} /> {backfillRunning ? "scoring…" : "backfill"}
            </button>
            <button
              type="button"
              onClick={runSweep}
              disabled={sweepRunning}
              data-testid="greylist-run-sweep-btn"
              className="flex items-center gap-1 px-2 py-1 border border-neutral-800 hover:bg-neutral-800 disabled:opacity-50 text-[10px] font-mono uppercase tracking-wider text-neutral-300"
              title="Force-run a failure-sweep cycle now (normally runs every 6h)"
            >
              <Play className="w-3 h-3" /> {sweepRunning ? "sweeping…" : "sweep"}
            </button>
            <button
              type="button"
              onClick={refresh}
              disabled={loading}
              data-testid="greylist-refresh-btn"
              className="p-1 border border-neutral-800 hover:bg-neutral-800 disabled:opacity-50 text-neutral-500"
              title="Refresh"
            >
              <RefreshCw className={`w-3 h-3 ${loading ? "animate-spin" : ""}`} />
            </button>
          </div>
        )}
      </div>

      {!open ? null : (
        <>
      {/* Tier counters */}
      <div className="flex items-center gap-3 mb-3 text-[10px] font-mono uppercase tracking-wider">
        <span className="text-neutral-600">tier:</span>
        <span className="text-rose-300">
          aggressive <span className="text-neutral-200 tabular-nums">{tierCounts.aggressive}</span>
        </span>
        <span className="text-amber-300">
          hybrid <span className="text-neutral-200 tabular-nums">{tierCounts.hybrid}</span>
        </span>
        <span className="text-neutral-500">
          standard <span className="text-neutral-300 tabular-nums">{tierCounts.standard}</span>
        </span>
        {mode === "live" && (
          <span className="ml-auto text-rose-400" data-testid="greylist-live-warn">
            ⚠ LIVE: overrides active on entry
          </span>
        )}
      </div>

      {items.length === 0 ? (
        <div
          className="text-center py-6 text-[10px] uppercase tracking-[0.2em] text-neutral-600"
          data-testid="greylist-empty"
        >
          {loading
            ? "loading…"
            : enabled
            ? `no creators with score ≥ ${minScore}. Lower the threshold or run a sweep.`
            : "greylist disabled in BotConfig"}
        </div>
      ) : (
        <ul className="space-y-1">
          {items.map((r) => (
            <GreylistRow
              key={r.creator}
              row={r}
              expanded={expanded === r.creator}
              onToggle={() =>
                setExpanded((prev) => (prev === r.creator ? null : r.creator))
              }
            />
          ))}
        </ul>
      )}

      {/* === Pattern PnL Analytics (Phase 2.6) === */}
      <div
        className="mt-4 border-t border-neutral-800 pt-3"
        data-testid="pattern-analytics-section"
      >
        <div className="flex items-center justify-between gap-2 mb-2 flex-wrap">
          <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.2em] text-neutral-500">
            <BarChart3 className="w-3 h-3" />
            Pattern PnL Analytics
            <span className="text-neutral-600 normal-case tracking-normal">
              {analytics?.totals?.n_trades ?? 0} trades · last {analytics?.days ?? analyticsDays}d
            </span>
          </div>
          <div className="flex items-center gap-2">
            <select
              value={analyticsDays}
              onChange={(e) => setAnalyticsDays(Number(e.target.value))}
              data-testid="analytics-days-select"
              className="px-1.5 py-0.5 bg-neutral-950 border border-neutral-800 text-neutral-200 font-mono text-[10px]"
            >
              <option value={1}>1d</option>
              <option value={7}>7d</option>
              <option value={30}>30d</option>
              <option value={90}>90d</option>
            </select>
            <select
              value={analyticsMode}
              onChange={(e) => setAnalyticsMode(e.target.value)}
              data-testid="analytics-mode-select"
              className="px-1.5 py-0.5 bg-neutral-950 border border-neutral-800 text-neutral-200 font-mono text-[10px]"
            >
              <option value="">all</option>
              <option value="live">live</option>
              <option value="paper">paper</option>
            </select>
          </div>
        </div>
        {!analytics || analytics.patterns.length === 0 ? (
          <div className="text-[10px] uppercase tracking-[0.2em] text-neutral-600 py-3 text-center">
            no closed trades in window
          </div>
        ) : (
          <div className="border border-neutral-800 overflow-hidden">
            <table className="w-full text-[10px] font-mono">
              <thead className="bg-neutral-900 text-neutral-500">
                <tr className="text-left">
                  <th className="px-2 py-1.5 font-normal uppercase tracking-wider">pattern</th>
                  <th className="px-2 py-1.5 font-normal uppercase tracking-wider text-right">n</th>
                  <th className="px-2 py-1.5 font-normal uppercase tracking-wider text-right">win%</th>
                  <th className="px-2 py-1.5 font-normal uppercase tracking-wider text-right">sl%</th>
                  <th className="px-2 py-1.5 font-normal uppercase tracking-wider text-right">μ pnl</th>
                  <th className="px-2 py-1.5 font-normal uppercase tracking-wider text-right hidden md:table-cell">med pnl</th>
                  <th className="px-2 py-1.5 font-normal uppercase tracking-wider text-right">total $</th>
                  <th className="px-2 py-1.5 font-normal uppercase tracking-wider text-right hidden lg:table-cell">best / worst</th>
                </tr>
              </thead>
              <tbody>
                {analytics.patterns.map((p) => {
                  const pm = PATTERN_META[p.pattern] || PATTERN_META.unknown;
                  const PI = pm.Icon;
                  const totalPnlClass =
                    p.total_pnl_usd > 0
                      ? "text-emerald-300"
                      : p.total_pnl_usd < 0
                      ? "text-rose-300"
                      : "text-neutral-500";
                  return (
                    <tr
                      key={p.pattern}
                      data-testid={`analytics-row-${p.pattern}`}
                      className="border-t border-neutral-800 hover:bg-neutral-900/50"
                    >
                      <td className="px-2 py-1.5">
                        <span
                          className={`inline-flex items-center gap-1 px-1.5 py-0.5 border text-[9px] uppercase tracking-[0.15em] ${pm.chip}`}
                        >
                          {p.pattern !== "unclassified" && <PI className="w-3 h-3" />}
                          {p.pattern === "unclassified" ? "UNCLASSIFIED" : pm.label}
                        </span>
                      </td>
                      <td className="px-2 py-1.5 text-right tabular-nums text-neutral-200">{p.n_trades}</td>
                      <td className={`px-2 py-1.5 text-right tabular-nums ${p.win_rate_pct >= 50 ? "text-emerald-300" : "text-neutral-300"}`}>
                        {p.win_rate_pct.toFixed(0)}%
                      </td>
                      <td className={`px-2 py-1.5 text-right tabular-nums ${p.sl_rate_pct >= 25 ? "text-rose-300" : "text-neutral-500"}`}>
                        {p.sl_rate_pct.toFixed(0)}%
                      </td>
                      <td className={`px-2 py-1.5 text-right tabular-nums ${p.mean_pnl_pct >= 0 ? "text-emerald-300" : "text-rose-300"}`}>
                        {p.mean_pnl_pct >= 0 ? "+" : ""}
                        {p.mean_pnl_pct.toFixed(1)}%
                      </td>
                      <td className="px-2 py-1.5 text-right tabular-nums text-neutral-400 hidden md:table-cell">
                        {p.median_pnl_pct >= 0 ? "+" : ""}
                        {p.median_pnl_pct.toFixed(1)}%
                      </td>
                      <td className={`px-2 py-1.5 text-right tabular-nums ${totalPnlClass}`}>
                        ${p.total_pnl_usd.toFixed(2)}
                      </td>
                      <td className="px-2 py-1.5 text-right tabular-nums text-neutral-500 hidden lg:table-cell">
                        <span className="text-emerald-400">+{p.best_pnl_pct.toFixed(0)}</span>
                        {" / "}
                        <span className="text-rose-400">{p.worst_pnl_pct.toFixed(0)}</span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        <div className="mt-1 text-[9px] font-mono text-neutral-600">
          sorted by total realized $ desc · "unclassified" = trades entered before pattern classifier was wired or with unknown creator pattern
        </div>
      </div>

      {/* === Blacklist sub-panel === Creators we've eliminated and WHY. */}
      <div className="mt-4 border-t border-neutral-800 pt-3">
        <button
          type="button"
          onClick={() => setShowBlacklist((v) => !v)}
          data-testid="blacklist-toggle"
          className="w-full flex items-center justify-between gap-2 text-[10px] uppercase tracking-[0.2em] text-neutral-500 hover:text-neutral-300 transition"
        >
          <span className="flex items-center gap-2">
            <Ban className="w-3 h-3" /> Blacklisted creators ({blacklist.length})
          </span>
          <span className="text-neutral-600 font-mono normal-case tracking-normal">
            untradeable · unpredictable · unknown — {showBlacklist ? "hide" : "show"}
          </span>
        </button>
        {showBlacklist && (
          <div className="mt-2" data-testid="blacklist-content">
            {blacklist.length === 0 ? (
              <div className="text-center py-4 text-[10px] uppercase tracking-[0.2em] text-neutral-600">
                no creators blacklisted yet
              </div>
            ) : (
              <ul className="space-y-1">
                {blacklist.slice(0, 25).map((b) => {
                  const pm = PATTERN_META[b.pattern] || PATTERN_META.unknown;
                  const PI = pm.Icon;
                  return (
                    <li
                      key={b.creator}
                      data-testid={`blacklist-row-${b.creator}`}
                      className="border border-neutral-800 px-3 py-2 flex items-center justify-between gap-3 bg-neutral-950/60"
                    >
                      <div className="flex items-center gap-2 min-w-0 flex-1">
                        <span
                          className={`flex items-center gap-1 text-[9px] font-mono uppercase tracking-[0.15em] px-1.5 py-0.5 border ${pm.chip}`}
                          title={pm.title}
                        >
                          <PI className="w-3 h-3" />
                          {pm.label}
                        </span>
                        <span className="text-[10px] font-mono text-neutral-400 truncate">
                          {short(b.creator)}
                        </span>
                        <span className="text-[10px] font-mono text-neutral-600 truncate hidden md:inline">
                          {(b.evidence || []).slice(0, 1).join(" · ")}
                        </span>
                      </div>
                      <div className="flex items-center gap-4 text-[10px] font-mono text-neutral-500 shrink-0">
                        <span className="tabular-nums" title="Lifetime tokens created">
                          C <span className="text-neutral-300">{b.tokens_created}</span>
                        </span>
                        <span className="tabular-nums" title="Lifetime tokens failed">
                          F <span className="text-neutral-300">{b.tokens_failed}</span>
                        </span>
                        <span className="tabular-nums" title="Failures with peak MC populated">
                          MC <span className="text-neutral-300">{b.n_failed_with_peak}</span>
                        </span>
                        <span className="hidden lg:inline">{fmtTime(b.last_seen)}</span>
                      </div>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        )}
      </div>

      <div className="mt-2 text-[10px] font-mono text-neutral-600">
        scoring: profitability 28% · predictability 20% · peak mc 25% · activity 13% · volume 9% · links 5% ·
        decay ~1%/hr · tiers at 45 (hybrid) and 70 (aggressive) ·
        F-band <span className="text-neutral-400">{config?.creator_greylist_min_fails ?? 2}–{config?.creator_greylist_max_fails ?? 100}</span>{" "}
        (outside band → stats kept, score suppressed)
      </div>
        </>
      )}
    </div>
  );
}
