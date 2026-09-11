import { useEffect, useRef, useState, memo } from "react";
import { createChart, BaselineSeries, CandlestickSeries, LineStyle, CrosshairMode } from "lightweight-charts";

const fmtUsd = (v) => `${v >= 0 ? "+" : "-"}$${Math.abs(Number(v)).toFixed(2)}`;
const fmtTime = (t, tf) => {
  const d = new Date(t * 1000);
  return tf === "1d" ? d.toLocaleDateString(undefined, { month: "short", day: "numeric" })
    : `${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })} ${d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}`;
};

// TradingView lightweight-charts equity chart. Line = baseline at 0 (green above / red below);
// Wicks = candlesticks of the OHLC equity buckets (empty buckets → no candle). Wheel zoom + drag pan are built in.
function EquityChart({ data, view, tf, height = 280 }) {
  const boxRef = useRef(null);
  const chartRef = useRef(null);
  const seriesRef = useRef(null);
  const [hover, setHover] = useState(null);

  useEffect(() => {
    const el = boxRef.current;
    if (!el) return undefined;
    const chart = createChart(el, {
      height,
      layout: { background: { color: "transparent" }, textColor: "#737373", fontFamily: "IBM Plex Mono, monospace", fontSize: 10, attributionLogo: false },
      grid: { vertLines: { color: "#171717" }, horzLines: { color: "#171717" } },
      rightPriceScale: { borderColor: "#262626", scaleMargins: { top: 0.1, bottom: 0.1 } },
      timeScale: { borderColor: "#262626", timeVisible: tf !== "1d", secondsVisible: false, rightOffset: 3 },
      crosshair: { mode: CrosshairMode.Normal, vertLine: { color: "#525252", labelBackgroundColor: "#262626" }, horzLine: { color: "#525252", labelBackgroundColor: "#262626" } },
      handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
      handleScale: { mouseWheel: true, pinch: true, axisPressedMouseMove: true },
      localization: { locale: "en-US", priceFormatter: (p) => `${p < 0 ? "-" : ""}$${Math.abs(p).toFixed(2)}` },
    });
    chartRef.current = chart;
    const ro = new ResizeObserver(() => chart.applyOptions({ width: el.clientWidth }));
    ro.observe(el);
    chart.applyOptions({ width: el.clientWidth });
    return () => { ro.disconnect(); chart.remove(); chartRef.current = null; seriesRef.current = null; };
  }, [height, tf]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return undefined;
    if (seriesRef.current) { chart.removeSeries(seriesRef.current); seriesRef.current = null; }
    const candles = data?.candles || [];
    const points = data?.points || [];
    const byT = new Map(candles.map((c) => [c.t, c]));
    let series;
    if (view === "wicks") {
      series = chart.addSeries(CandlestickSeries, {
        upColor: "#10b981", downColor: "#ef4444", borderUpColor: "#10b981", borderDownColor: "#ef4444",
        wickUpColor: "#10b981", wickDownColor: "#ef4444", priceLineVisible: false,
      });
      series.setData(candles.map((c) => ({ time: c.t, open: c.open, high: c.high, low: c.low, close: c.close })));
    } else {
      series = chart.addSeries(BaselineSeries, {
        baseValue: { type: "price", price: 0 },
        topLineColor: "#10b981", topFillColor1: "rgba(16,185,129,0.28)", topFillColor2: "rgba(16,185,129,0.02)",
        bottomLineColor: "#ef4444", bottomFillColor1: "rgba(239,68,68,0.02)", bottomFillColor2: "rgba(239,68,68,0.28)",
        lineWidth: 2, priceLineVisible: false,
      });
      // one value per timestamp (lightweight-charts needs strictly increasing time)
      const dedup = [];
      for (const p of points) {
        if (dedup.length && dedup[dedup.length - 1].time === p.t) dedup[dedup.length - 1].value = p.equity;
        else dedup.push({ time: p.t, value: p.equity });
      }
      series.setData(dedup);
    }
    series.createPriceLine({ price: 0, color: "#525252", lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: "0" });
    seriesRef.current = series;
    chart.timeScale().fitContent();
    const onMove = (param) => {
      if (!param?.time || !param.point) { setHover(null); return; }
      const c = byT.get(param.time);
      const v = param.seriesData?.get(series);
      setHover({ t: param.time, c, v: v && (v.close ?? v.value) });
    };
    chart.subscribeCrosshairMove(onMove);
    return () => chart.unsubscribeCrosshairMove(onMove);
  }, [data, view]);

  return (
    <div className="relative" data-testid="equity-chart">
      <div ref={boxRef} className="w-full" style={{ height }} />
      {hover && (
        <div className="absolute left-2 top-1 pointer-events-none text-[10px] font-mono text-neutral-300 bg-neutral-950/85 border border-neutral-800 px-2 py-1 leading-4" data-testid="equity-tooltip">
          <div className="text-neutral-500">{fmtTime(hover.t, tf)}</div>
          {hover.c ? (
            <>
              <div>O {fmtUsd(hover.c.open)} H {fmtUsd(hover.c.high)} L {fmtUsd(hover.c.low)} C {fmtUsd(hover.c.close)}</div>
              <div className={hover.c.pnl_usd >= 0 ? "text-emerald-400" : "text-red-400"}>bucket {fmtUsd(hover.c.pnl_usd)} · {hover.c.n} fills</div>
              <div className="text-neutral-500">paper {fmtUsd(hover.c.paper_usd)} · live {fmtUsd(hover.c.live_usd)}</div>
            </>
          ) : hover.v != null ? (
            <div>equity {fmtUsd(hover.v)}</div>
          ) : null}
        </div>
      )}
    </div>
  );
}

export default memo(EquityChart);
