import { LineChart, Line, ResponsiveContainer, Tooltip } from "recharts";
import { TrendingUp, TrendingDown } from "lucide-react";

export default function PLSummaryCard({ pl }) {
  const daily = pl?.daily_pnl_usd ?? 0;
  const cum = pl?.cumulative_usd ?? 0;
  const series = (pl?.series || []).map((d, i) => ({
    x: i,
    v: d.cumulative_usd,
    pnl: d.pnl_usd,
  }));
  const positive = daily >= 0;

  return (
    <div className="control-card flex flex-col gap-3" data-testid="pl-summary-card">
      <div className="flex items-center justify-between">
        <span className="text-[10px] uppercase tracking-[0.2em] text-neutral-500">P/L Today</span>
        {positive ? <TrendingUp className="w-3 h-3 text-emerald-500" /> : <TrendingDown className="w-3 h-3 text-red-500" />}
      </div>
      <div className={`text-3xl font-mono font-semibold ${positive ? "text-emerald-400" : "text-red-400"}`} data-testid="daily-pnl">
        {positive ? "+" : ""}${daily.toFixed(2)}
      </div>
      <div className="text-xs font-mono text-neutral-400">
        7-day cumulative: <span className={cum >= 0 ? "text-emerald-400" : "text-red-400"} data-testid="cumulative-pnl">
          {cum >= 0 ? "+" : ""}${cum.toFixed(2)}
        </span>
      </div>
      <div className="h-16 -mx-1" data-testid="pl-sparkline">
        {series.length > 1 ? (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={series}>
              <Line
                type="monotone"
                dataKey="v"
                stroke={cum >= 0 ? "#10b981" : "#ef4444"}
                strokeWidth={1.5}
                dot={false}
                isAnimationActive={false}
              />
              <Tooltip
                contentStyle={{ background: "#0a0a0a", border: "1px solid #262626", fontSize: 11, fontFamily: "IBM Plex Mono" }}
                labelStyle={{ display: "none" }}
                formatter={(v) => [`$${Number(v).toFixed(2)}`, "Cum"]}
              />
            </LineChart>
          </ResponsiveContainer>
        ) : (
          <div className="h-full flex items-center justify-center text-[10px] uppercase tracking-[0.2em] text-neutral-600">
            no closed trades yet
          </div>
        )}
      </div>
    </div>
  );
}
