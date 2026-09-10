import { useState, useEffect, memo } from "react";
import { LineChart, Line, BarChart, Bar, XAxis, YAxis, ReferenceLine, ResponsiveContainer, Tooltip } from "recharts";
import { TrendingUp, TrendingDown, RotateCcw, CandlestickChart, Activity } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";

const TIMEFRAMES = [300, 900, 1800, 3600, 14400, 43200, 86400];
const CANDLES = 60;

// One candle of the cumulative-P/L "price": body open→close, wicks to high/low. Recharts gives us the pixel box of the
// [low, high] range, so we scale the body inside it. Flat (no fills) candles draw as a thin doji line.
function Candle({ x, y, width, height, payload }) {
  const { open, close, high, low } = payload;
  const up = close >= open;
  const color = payload.trades === 0 ? "#3f3f46" : up ? "#10b981" : "#ef4444";
  const span = high - low;
  const px = (v) => (span > 0 ? y + (high - v) * (height / span) : y);
  const top = px(Math.max(open, close));
  const bodyH = Math.max(1, Math.abs(px(Math.min(open, close)) - top));
  const cx = x + width / 2;
  const bw = Math.max(1, width * 0.7);
  return (
    <g>
      <line x1={cx} x2={cx} y1={y} y2={y + height} stroke={color} strokeWidth={1} />
      <rect x={cx - bw / 2} y={top} width={bw} height={bodyH} fill={up ? color : color} stroke={color} />
    </g>
  );
}

function PLSummaryCard({ pl, status, onReset }) {
  const [confirming, setConfirming] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [chart, setChart] = useState(() => localStorage.getItem("ui.pl.chart") || "line");
  const flipChart = () => setChart((c) => { const n = c === "line" ? "bars" : "line"; localStorage.setItem("ui.pl.chart", n); return n; });
  // Market-style candles of OUR trading: the 7-day cumulative P/L is the price. Candle width is fixed, so the
  // timeframe decides how much history fits (60 × 5m = 5 h … 7 × 1d). Wicks = intra-period high/low of the running total.
  const [tf, setTf] = useState(() => Number(localStorage.getItem("ui.pl.tf")) || 3600);
  const [buckets, setBuckets] = useState([]);
  const pickTf = (v) => { setTf(v); localStorage.setItem("ui.pl.tf", String(v)); };
  useEffect(() => {
    let alive = true;
    const load = () => api.plBuckets(tf, CANDLES).then((d) => alive && setBuckets(d?.buckets || [])).catch(() => {});
    load();
    const id = setInterval(load, 30000);
    return () => { alive = false; clearInterval(id); };
  }, [tf, pl?.cumulative_usd, pl?.daily_pnl_usd]);
  const hasFills = buckets.some((b) => b.trades > 0);
  const tfLabel = (s) => (s >= 86400 ? `${s / 86400}d` : s >= 3600 ? `${s / 3600}h` : `${s / 60}m`);
  const fmtT = (t) => {
    const d = new Date(t * 1000);
    return tf >= 86400 ? d.toLocaleDateString(undefined, { month: "short", day: "numeric" })
      : `${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })} ${d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}`;
  };
  // one tick per day boundary (local midnight) — the window is 7 days, so ~7 ticks at every resolution
  const dayTicks = buckets.filter((b, i) => i > 0 && new Date(b.t * 1000).getDate() !== new Date(buckets[i - 1].t * 1000).getDate()).map((b) => b.t);
  const tip = { background: "#0a0a0a", border: "1px solid #262626", fontSize: 11, fontFamily: "IBM Plex Mono" };

  const daily = pl?.daily_pnl_usd ?? 0;
  const cum = pl?.cumulative_usd ?? 0;
  const positive = daily >= 0;

  const doReset = async () => {
    setResetting(true);
    try {
      const res = await api.paperReset();
      toast.success(`Cleared ${res.deleted_trades} paper trades · kill-switch reset`);
      setConfirming(false);
      onReset && onReset();
    } catch (e) {
      const msg = e?.response?.data?.detail || e.message || "reset failed";
      toast.error(msg);
    } finally {
      setResetting(false);
    }
  };

  return (
    <div className="control-card flex flex-col gap-3" data-testid="pl-summary-card">
      <div className="flex items-center justify-between">
        <span className="text-[10px] uppercase tracking-[0.2em] text-neutral-500">P/L Today</span>
        <div className="flex items-center gap-2">
          {positive ? <TrendingUp className="w-3 h-3 text-emerald-500" /> : <TrendingDown className="w-3 h-3 text-red-500" />}
          <button
            onClick={flipChart}
            title={chart === "line" ? "Switch to candlesticks (open/high/low/close of the running P/L)" : "Switch to cumulative line"}
            data-testid="pl-chart-toggle"
            className="text-[10px] font-mono uppercase tracking-[0.15em] text-neutral-500 hover:text-blue-300 inline-flex items-center gap-1"
          >
            {chart === "line" ? <CandlestickChart className="w-3 h-3" /> : <Activity className="w-3 h-3" />} {chart === "line" ? "candles" : "line"}
          </button>
          <button
            onClick={() => setConfirming(true)}
            title="Clear paper trades + reset 1d/7d view (live trades preserved on-chain)"
            data-testid="reset-paper-btn"
            className="text-[10px] font-mono uppercase tracking-[0.15em] text-neutral-500 hover:text-amber-400 disabled:opacity-30 disabled:cursor-not-allowed inline-flex items-center gap-1"
          >
            <RotateCcw className="w-3 h-3" /> Reset
          </button>
        </div>
      </div>
      <div className={`text-3xl font-mono font-semibold ${positive ? "text-emerald-400" : "text-red-400"}`} data-testid="daily-pnl">
        {positive ? "+" : ""}${daily.toFixed(2)}
      </div>
      <div className="text-xs font-mono text-neutral-400">
        7-day cumulative: <span className={cum >= 0 ? "text-emerald-400" : "text-red-400"} data-testid="cumulative-pnl">
          {cum >= 0 ? "+" : ""}${cum.toFixed(2)}
        </span>
      </div>
      <div className="flex items-center gap-1 text-[9px] font-mono" data-testid="pl-timeframes">
        {TIMEFRAMES.map((s) => (
          <button
            key={s}
            type="button"
            onClick={() => pickTf(s)}
            data-testid={`pl-tf-${tfLabel(s)}`}
            className={`px-1.5 py-0.5 border uppercase transition-colors ${tf === s ? "border-blue-500 text-blue-300 bg-blue-950/40" : "border-neutral-800 text-neutral-500 hover:text-neutral-300"}`}
          >
            {tfLabel(s)}
          </button>
        ))}
        <span className="ml-auto text-neutral-600">{buckets.length} × {tfLabel(tf)} · 7d cum</span>
      </div>
      <div className="h-24 -mx-1" data-testid="pl-sparkline">
        {!hasFills ? (
          <div className="h-full flex items-center justify-center text-[10px] uppercase tracking-[0.2em] text-neutral-600">
            no closed trades in the last 7 days
          </div>
        ) : chart === "bars" ? (
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={buckets} barCategoryGap={tf >= 14400 ? "20%" : "15%"} data-testid="pl-daily-bars">
              <YAxis hide domain={["dataMin", "dataMax"]} />
              <XAxis dataKey="t" ticks={dayTicks} tickFormatter={(t) => new Date(t * 1000).toLocaleDateString(undefined, { weekday: "short" })} tick={{ fontSize: 9, fill: "#525252", fontFamily: "IBM Plex Mono" }} axisLine={{ stroke: "#262626" }} tickLine={false} interval={0} height={14} />
              <ReferenceLine y={0} stroke="#404040" strokeDasharray="2 2" />
              <Bar dataKey="range" isAnimationActive={false} shape={<Candle />} />
              <Tooltip
                cursor={{ fill: "#262626", opacity: 0.4 }}
                contentStyle={tip}
                labelFormatter={(_, p) => (p?.[0]?.payload ? fmtT(p[0].payload.t) : "")}
                formatter={(_v, _n, p) => {
                  const c = p?.payload || {};
                  const f = (v) => `${v >= 0 ? "+" : ""}${Number(v).toFixed(2)}`;
                  return [`O ${f(c.open)} H ${f(c.high)} L ${f(c.low)} C ${f(c.close)} · ${f(c.pnl_usd)} in ${c.trades ?? 0} fills (live ${f(c.live_usd)} / paper ${f(c.paper_usd)})`, tfLabel(tf)];
                }}
              />
            </BarChart>
          </ResponsiveContainer>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={buckets}>
              <YAxis hide domain={[(min) => Math.min(0, min), (max) => Math.max(0, max)]} />
              <XAxis dataKey="t" ticks={dayTicks} tickFormatter={(t) => new Date(t * 1000).toLocaleDateString(undefined, { weekday: "short" })} tick={{ fontSize: 9, fill: "#525252", fontFamily: "IBM Plex Mono" }} axisLine={{ stroke: "#262626" }} tickLine={false} interval={0} height={14} />
              <ReferenceLine y={0} stroke="#404040" strokeDasharray="2 2" />
              <Line type="stepAfter" dataKey="cumulative_usd" stroke={(buckets[buckets.length - 1]?.cumulative_usd ?? 0) >= 0 ? "#10b981" : "#ef4444"} strokeWidth={1.5} dot={false} isAnimationActive={false} />
              <Tooltip
                contentStyle={tip}
                labelFormatter={(_, p) => (p?.[0]?.payload ? fmtT(p[0].payload.t) : "")}
                formatter={(v, _n, p) => [`$${Number(v).toFixed(2)} (bucket ${Number(p?.payload?.pnl_usd ?? 0) >= 0 ? "+" : ""}${Number(p?.payload?.pnl_usd ?? 0).toFixed(2)}, ${p?.payload?.trades ?? 0} fills)`, "Cum"]}
              />
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>

      {confirming && (
        <div className="fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-4" data-testid="reset-confirm-dialog">
          <div className="w-full max-w-sm control-card">
            <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.2em] text-amber-400 mb-3">
              <RotateCcw className="w-3 h-3" /> Reset Paper Mode
            </div>
            <p className="text-sm text-neutral-300 mb-2">
              This will <span className="text-amber-400">clear the 1d/7d PnL view</span>:
            </p>
            <ul className="text-xs font-mono text-neutral-400 list-disc list-inside space-y-1 mb-4">
              <li>Paper trades → <span className="text-red-400">deleted</span></li>
              <li>Live trade history → <span className="text-neutral-300">hidden from view (preserved on-chain)</span></li>
              <li>Daily P/L → $0.00</li>
              <li>Kill-switch → reset</li>
              <li>Re-entry watchlist → cleared</li>
            </ul>
            <p className="text-[11px] text-neutral-500 mb-4">
              Your <span className="text-emerald-400">live trade rows stay in the DB</span> for audit.
              Active live positions keep running. The counters and 7-day chart simply start fresh from now.
            </p>
            <div className="flex gap-2">
              <button
                onClick={() => setConfirming(false)}
                data-testid="reset-cancel-btn"
                className="flex-1 px-3 py-2 border border-neutral-700 text-neutral-300 hover:bg-neutral-900 font-mono text-xs uppercase tracking-[0.15em]"
              >
                Cancel
              </button>
              <button
                onClick={doReset}
                disabled={resetting}
                data-testid="reset-confirm-btn"
                className="flex-1 px-3 py-2 border border-amber-700 text-amber-200 bg-amber-950 hover:bg-amber-900 font-mono text-xs uppercase tracking-[0.15em] disabled:opacity-40"
              >
                {resetting ? "Resetting…" : "Reset Now"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default memo(PLSummaryCard);
