import { memo, useEffect, useRef, useState } from "react";
import { createChart, CandlestickSeries, CrosshairMode, createSeriesMarkers } from "lightweight-charts";
import { api } from "@/lib/api";

const TFS = [["1m", 60], ["5m", 300], ["15m", 900], ["1h", 3600]];
const fmt = (v) => (v == null ? "—" : v < 1e-4 ? v.toExponential(3) : v.toFixed(6));

/** Candles from OUR tick feed (tick_paths) — the chart for venues DexScreener has no candle data for (Robinhood v4 pools). */
function TokenChart({ chain, mint, height = 420 }) {
  const [tf, setTf] = useState(60);
  const [data, setData] = useState(null);
  const [hover, setHover] = useState(null);
  const boxRef = useRef(null), chartRef = useRef(null), seriesRef = useRef(null), markersRef = useRef(null);

  useEffect(() => {
    let alive = true;
    const load = () => api.tokenCandles(chain, mint, tf).then((d) => alive && setData(d)).catch(() => alive && setData({ candles: [], error: true }));
    load();
    const id = setInterval(load, 15000);
    return () => { alive = false; clearInterval(id); };
  }, [chain, mint, tf]);

  useEffect(() => {
    if (!boxRef.current) return undefined;
    const chart = createChart(boxRef.current, {
      height, layout: { background: { color: "#000" }, textColor: "#737373", fontFamily: "JetBrains Mono, monospace", fontSize: 10 },
      grid: { vertLines: { color: "#171717" }, horzLines: { color: "#171717" } },
      rightPriceScale: { borderColor: "#262626" }, timeScale: { borderColor: "#262626", timeVisible: true, secondsVisible: false },
      crosshair: { mode: CrosshairMode.Normal }, handleScroll: true, handleScale: true,
      localization: { locale: "en-US", priceFormatter: fmt },
    });
    const series = chart.addSeries(CandlestickSeries, {
      upColor: "#10b981", downColor: "#ef4444", borderUpColor: "#10b981", borderDownColor: "#ef4444", wickUpColor: "#10b981", wickDownColor: "#ef4444",
      priceFormat: { type: "custom", formatter: fmt, minMove: 1e-12 },
    });
    chartRef.current = chart; seriesRef.current = series; markersRef.current = createSeriesMarkers(series, []);
    const onMove = (p) => { const v = p?.seriesData?.get(series); setHover(v && p.time ? { t: p.time, ...v } : null); };
    chart.subscribeCrosshairMove(onMove);
    const ro = new ResizeObserver(() => chart.applyOptions({ width: boxRef.current?.clientWidth || 300 }));
    ro.observe(boxRef.current);
    return () => { chart.unsubscribeCrosshairMove(onMove); ro.disconnect(); chart.remove(); chartRef.current = null; seriesRef.current = null; };
  }, [height]);

  useEffect(() => {
    const s = seriesRef.current, c = chartRef.current;
    if (!s || !c || !data) return;
    s.setData((data.candles || []).map((k) => ({ time: k.t, open: k.o, high: k.h, low: k.l, close: k.c })));
    markersRef.current?.setMarkers((data.marks || []).filter((m) => m.t).sort((a, b) => a.t - b.t).map((m) => ({
      time: m.t, position: m.kind === "entry" ? "belowBar" : "aboveBar", color: m.kind === "entry" ? "#38bdf8" : (m.pnl_pct ?? 0) >= 0 ? "#10b981" : "#ef4444",
      shape: m.kind === "entry" ? "arrowUp" : "arrowDown", text: m.kind === "entry" ? "buy" : `sell ${m.pnl_pct != null ? `${m.pnl_pct >= 0 ? "+" : ""}${m.pnl_pct.toFixed(0)}%` : ""}`,
    })));
    c.timeScale().fitContent();
  }, [data]);

  const last = data?.candles?.length ? data.candles[data.candles.length - 1] : null;
  return (
    <div className="flex flex-col h-full" data-testid="token-chart">
      <div className="flex items-center justify-between px-2 py-1 border-b border-neutral-800 text-[10px] font-mono">
        <span className="uppercase tracking-[0.15em] text-neutral-500">
          our feed · {data?.n_samples ?? 0} ticks · {data?.quote || (chain === "rh" ? "ETH" : "SOL")}
        </span>
        <span className="text-neutral-300" data-testid="token-chart-readout">
          {hover ? `O ${fmt(hover.open)} H ${fmt(hover.high)} L ${fmt(hover.low)} C ${fmt(hover.close)}` : last ? `last ${fmt(last.c)}` : data?.error ? "chart unavailable" : "loading…"}
        </span>
        <div className="flex gap-1">
          {TFS.map(([l, v]) => (
            <button key={v} onClick={() => setTf(v)} data-testid={`token-chart-tf-${l}`}
              className={`px-1.5 py-0.5 border transition-colors duration-100 ${tf === v ? "border-neutral-400 text-neutral-100" : "border-neutral-800 text-neutral-500 hover:text-neutral-300"}`}>{l}</button>
          ))}
        </div>
      </div>
      <div ref={boxRef} className="w-full" style={{ height }} />
      {data && !data.candles?.length && (
        <div className="px-2 py-2 text-[10px] font-mono text-neutral-500">
          No ticks recorded for this token yet — the feed writes a sample on every on-chain trade it sees while the token is tracked.
        </div>
      )}
    </div>
  );
}

export default memo(TokenChart);
