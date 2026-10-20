"""Gate Replay — paste a mint, see every gate's LIVE reading vs its threshold with pass/fail, plus why we entered /
exited (decision ledger + trade rows). Read-only: reuses the scanner's own metric + threshold functions so the numbers
are the ones the bot acts on."""
from __future__ import annotations

import time
from collections import deque

import cost_gate
import creator_verdict
import flow
import pumpfun
import pumpswap
import solana_client
from scanner import MomentumScanner, _mc_velocity, curve_is_complete, velocity_pct_strict

LAMPORTS = 1_000_000_000


def _row(gate: str, reading, threshold, ok: bool | None, note: str = "", scope: str = "both") -> dict:
    return {"gate": gate, "reading": reading, "threshold": threshold, "pass": ok, "note": note, "scope": scope}


def _fmt(v, unit=""):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:,.2f}{unit}"
    return f"{v}{unit}"


async def _live_state(mint: str, b: dict | None) -> tuple[str, dict | None, str]:
    """(protocol, curve/pool state, pool) from chain; falls back to the bucket's protocol when reads fail."""
    proto = (b or {}).get("protocol") or "pumpfun"
    pool = (b or {}).get("pumpswap_pool") or ""
    try:
        cs = await pumpfun.fetch_bonding_curve_state(mint)
    except Exception:
        cs = None
    if cs and not cs.get("complete"):
        return "pumpfun", cs, ""
    try:
        pool = pool or (await pumpswap.find_pool_for_mint(mint)) or ""
        ps = await pumpswap.fetch_pool_state(pool) if pool else None
    except Exception:
        ps = None
    if ps:
        return "pumpswap", ps, pool
    return proto, cs, pool


def _synth_bucket(mint: str, doc: dict | None, proto: str, live: dict | None, now: float) -> dict:
    """Rebuild a scanner bucket from the persisted tick path when the token is not tracked right now."""
    b: dict = {"mint": mint, "protocol": proto, "symbol": (doc or {}).get("symbol"), "creator": (doc or {}).get("creator"),
               "start": (doc or {}).get("start") or now, "first_seen_price_sol": (doc or {}).get("first_price") or 0.0,
               "usd_market_cap": (doc or {}).get("mc_usd") or 0.0, "curve_fill_pct": (doc or {}).get("curve_fill_pct") or 0.0,
               "buyers": set(), "buy_events": deque(maxlen=240), "sell_events": deque(maxlen=240),
               "price_samples": deque(maxlen=120), "mc_samples": deque(maxlen=12), "decisions": (doc or {}).get("decisions") or []}
    for ts, px in (doc or {}).get("samples") or []:
        b["price_samples"].append((float(ts), float(px)))
    for ts, sol, who in (doc or {}).get("buys") or []:
        b["buy_events"].append((float(ts), float(sol) * LAMPORTS, who))
        b["buyers"].add(who)
    if b["price_samples"]:
        b["last_price_sol"] = b["price_samples"][-1][1]
    if live:
        if proto == "pumpfun":
            b["last_vsr_lamports"] = int(live.get("virtual_sol_reserves") or 0)
            b["last_real_sol_lamports"] = int(live.get("real_sol_reserves") or 0)
            b["curve_fill_pct"] = min(100.0, b["last_real_sol_lamports"] / 85 / LAMPORTS * 100)
            b["complete"] = bool(live.get("complete"))
        else:
            q = int(live.get("real_quote_reserves", live.get("quote_reserves")) or 0)
            b["last_vsr_lamports"] = q
            b["last_real_sol_lamports"] = q
    return b


async def replay(state, db, mint: str) -> dict:
    cfg = state.config
    now = time.time()
    tracked = state.tracking.get(mint)
    doc = await db.tick_paths.find_one({"_id": f"sol:{mint}"}, {"_id": 0})
    proto, live, pool = await _live_state(mint, tracked)
    b = tracked if tracked is not None else _synth_bucket(mint, doc, proto, live, now)
    if tracked is not None and live and proto == "pumpfun":
        b.setdefault("last_vsr_lamports", int(live.get("virtual_sol_reserves") or 0))
    sol_usd = float(await solana_client.get_sol_usd_price() or 0.0)
    sc = MomentumScanner(state)
    curve_state = None
    if live and proto == "pumpswap":
        curve_state = {"virtual_sol_reserves": live["quote_reserves"], "virtual_token_reserves": live["base_reserves"],
                       "real_sol_reserves": live.get("real_quote_reserves", live["quote_reserves"]), "complete": False}
    elif live:
        curve_state = live
    m = sc.score(b, curve_state, now)
    age_min = m["age_s"] / 60.0
    band = MomentumScanner.classify_band(b, cfg, now)
    rows: list[dict] = []

    # ---- identity -------------------------------------------------------------------------------------------------
    rows.append(_row("pump.fun mint (…pump)", mint[-6:], "ends in 'pump'" if cfg.pump_suffix_only else "off",
                     mint.endswith("pump") or not cfg.pump_suffix_only, "pump_suffix_only"))
    rows.append(_row("protocol / band", f"{proto} → {band or 'no band'}", "New = curve, Seasoned = PumpSwap", band is not None,
                     f"age {age_min:.1f} min · New {cfg.band_new_min_age_min:g}–{cfg.band_new_max_age_min:g} · Seasoned {cfg.band_seasoned_min_age_min:g}–{cfg.band_seasoned_max_age_min:g} (from graduation)"))
    if proto == "pumpfun":
        done = curve_is_complete(b)
        rows.append(_row("curve complete", f"{float(b.get('curve_fill_pct') or 0):.1f}% filled", "< 100 %", not done,
                         "graduation pending — waits for the PumpSwap pool" if done else "", "new"))
        if live and live.get("is_mayhem") is not None:
            rows.append(_row("mayhem mode", "yes" if live.get("is_mayhem") else "no", "no" if cfg.skip_mayhem_coins else "allowed",
                             not (live.get("is_mayhem") and cfg.skip_mayhem_coins), "skip_mayhem_coins", "new"))
    else:
        why = pumpswap.unverified_pool_reason(live, mint) if live else "pool state unreadable"
        rows.append(_row("pool verified (pump.fun graduation)", (pool or "?")[:8] + "…", "creator = migration authority, token on base side",
                         why is None, why or "", "seasoned"))

    # ---- scanner metrics vs band thresholds ----------------------------------------------------------------------
    g_band = band or ("seasoned" if proto == "pumpswap" else "new")
    g = state.smart_wallets.relax_gates(sc._gates(cfg, g_band), b, now) if getattr(state, "smart_wallets", None) else sc._gates(cfg, g_band)
    mc = float(b.get("usd_market_cap") or 0.0)
    if not mc and sol_usd and b.get("last_price_sol"):
        mc = float(b["last_price_sol"]) * 1e9 * sol_usd
    if g_band == "new":
        if cfg.scanner_min_mc_usd_new > 0 or cfg.scanner_max_mc_usd_new > 0:
            rows.append(_row("market cap (USD)", f"${mc:,.0f}", f"${cfg.scanner_min_mc_usd_new:,.0f} – {('$%s' % format(cfg.scanner_max_mc_usd_new, ',.0f')) if cfg.scanner_max_mc_usd_new else '∞'}",
                             (cfg.scanner_min_mc_usd_new <= 0 or mc >= cfg.scanner_min_mc_usd_new) and (cfg.scanner_max_mc_usd_new <= 0 or mc <= cfg.scanner_max_mc_usd_new),
                             f"graduation ≈ ${411 * sol_usd:,.0f} today" if sol_usd else "", "new"))
        lo, hi = float(cfg.scanner_min_curve_fill_pct_new or 0), float(cfg.scanner_max_curve_fill_pct_new or 0)
        fill = float(b.get("curve_fill_pct") or 0)
        if lo or hi:
            rows.append(_row("curve fill %", f"{fill:.1f}%", f"{lo:g} – {hi or 100:g} %", (not lo or fill >= lo) and (not hi or fill <= hi), "", "new"))
        vmin = float(cfg.scanner_min_mc_velocity_5m_pct_new or 0)
        ps = b.get("price_samples") or ()
        v5 = _mc_velocity(ps, now) if len(ps) >= 2 else _mc_velocity(b.get("mc_samples") or (), now)
        rows.append(_row("MC velocity (5 m)", f"{v5:+.1f}%", f"≥ {vmin:g}%" if vmin else "off", v5 >= vmin if vmin else None, "tape price samples", "new"))
        rows.append(_row("buy events on tape", len(b.get("buy_events") or ()), "≥ 1", bool(b.get("buy_events")), "", "new"))
        rows.append(_row("rolling growth", f"{m['growth_pct_rolling']:+.1f}%", f"≥ {g['min_growth_pct']:g}% over {int(cfg.scanner_growth_lookback_s)}s",
                         m["growth_pct_rolling"] >= g["min_growth_pct"], "", "new"))
        rows.append(_row("recent inflow", f"{m['recent_inflow_sol']:.2f} SOL", f"≥ {g['min_inflow_sol']:g} SOL / {int(cfg.scanner_recent_inflow_window_s)}s",
                         m["recent_inflow_sol"] >= g["min_inflow_sol"], "", "new"))
        rows.append(_row("new buyers (window)", m["new_buyers_recent"], f"≥ {g['min_new_buyers']}", m["new_buyers_recent"] >= g["min_new_buyers"], "", "new"))
        rows.append(_row("unique buyers total", m.get("unique_buyers_total"), f"≥ {cfg.min_buyers_for_entry_new}",
                         int(m.get("unique_buyers_total") or 0) >= int(cfg.min_buyers_for_entry_new), "min_buyers_for_entry_new (× regime mult at entry)", "new"))
        rows.append(_row("curve liquidity", f"{m['real_sol_reserves']:.2f} SOL", f"≥ {g['min_liquidity_sol']:g} SOL", m["real_sol_reserves"] >= g["min_liquidity_sol"], "", "new"))
    else:
        mx = float(cfg.scanner_max_mc_usd_seasoned or 0)
        rows.append(_row("market cap (USD)", f"${mc:,.0f}", f"${cfg.scanner_min_mc_usd_seasoned:,.0f} – {('$%s' % format(mx, ',.0f')) if mx else '∞'}",
                         mc >= cfg.scanner_min_mc_usd_seasoned and (mx <= 0 or mc <= mx), "", "seasoned"))
        v5 = _mc_velocity(b.get("mc_samples") or (), now)
        rows.append(_row("MC velocity (5 m)", f"{v5:+.1f}%", f"≥ {cfg.scanner_min_mc_velocity_5m_pct_seasoned:g}%",
                         v5 >= cfg.scanner_min_mc_velocity_5m_pct_seasoned, "Pump.fun API MC samples", "seasoned"))
        rows.append(_row("rolling growth", f"{m['growth_pct_rolling']:+.1f}%", f"≥ {g['min_growth_pct']:g}%", m["growth_pct_rolling"] >= g["min_growth_pct"], "", "seasoned"))
        rows.append(_row("pool liquidity", f"{m['real_sol_reserves']:.2f} SOL", f"≥ {g['min_liquidity_sol']:g} SOL", m["real_sol_reserves"] >= g["min_liquidity_sol"], "", "seasoned"))
        rows.append(_row("unique buyers total", m.get("unique_buyers_total"), f"≥ {cfg.min_buyers_for_entry}",
                         int(m.get("unique_buyers_total") or 0) >= int(cfg.min_buyers_for_entry), "min_buyers_for_entry", "seasoned"))
        lt = b.get("last_trade_ts")
        max_age = float(getattr(cfg, "seasoned_max_last_trade_s", 20.0) or 20.0)
        rows.append(_row("last print age", f"{(now - lt):.0f}s" if lt else "—", f"≤ {max_age:g}s", (now - lt) <= max_age if lt else None, "live tape required", "seasoned"))

    # ---- entry-time gates (both bands) --------------------------------------------------------------------------
    fmin = float(getattr(cfg, "scanner_min_flow_ratio_pct", 0) or 0)
    fr = flow.flow_ratio_pct(b, now, 30.0, sol=True) if fmin > 0 else None
    rows.append(_row("net flow / liquidity", f"{fr:+.2f}%" if fr is not None else "—", f"≥ {fmin:g}%" if fmin else "off", (fr >= fmin if fr is not None else None) if fmin else None,
                     "buys − sells over 30 s ÷ pool SOL" + ("" if fr is not None else " · unknown here (no prints in window / tape-blind pool) — at entry a silent CURVE counts as 0 %")))
    evmin = float(getattr(cfg, "scanner_entry_velocity_min_pct", 0) or 0)
    ev = velocity_pct_strict(b.get("price_samples") or (), now, int(getattr(cfg, "scanner_entry_velocity_window_s", 30) or 30)) if evmin else None
    rows.append(_row("entry velocity", f"{ev:+.2f}%" if ev is not None else "—", f"≥ {evmin:g}% / {int(getattr(cfg, 'scanner_entry_velocity_window_s', 30) or 30)}s" if evmin else "off",
                     (ev is not None and ev >= evmin) if evmin else None, "price now vs price one window ago"))
    rc = int(b.get("reply_count") or 0)
    rows.append(_row("socials / replies", f"{rc} replies", f"≥ {int(cfg.gate_min_reply_count)} + a social link", rc >= int(cfg.gate_min_reply_count) if b.get("reply_count") is not None else None, "gate_min_reply_count"))

    # ---- cost gate --------------------------------------------------------------------------------------------------
    depth_usd = m["real_sol_reserves"] * sol_usd
    size = min(float(cfg.max_trade_usd), float(getattr(cfg, "discovery_clip_usd", cfg.max_trade_usd) or cfg.max_trade_usd))
    sl_pct = float(((getattr(cfg, "book_exits", None) or {}).get("scalp" if g_band == "new" else "hunt") or {}).get("stop_loss_pct") or 16.0)
    cq = cost_gate.quote(size_usd=size, r_usd=max(0.01, size * sl_pct / 100.0), first_target_r=None, protocol=proto,
                         entry_slip_bps=int(getattr(cfg, "max_slippage_bps", 300) or 300), exit_slip_bps=int(getattr(cfg, "auto_exit_slip_base_bps", 300) or 300),
                         fee_usd_round_trip=float(getattr(cfg, "replay_fee_usd_round_trip", 0.07) or 0.07), ladder=False, depth_usd=depth_usd, cfg=cfg) if depth_usd > 0 else None
    if cq:
        bd = cq.get("cost_breakdown") or {}
        rows.append(_row("cost gate (round trip)", f"{cq['expected_cost_pct']:.2f}% on ${size:,.0f}", f"≤ {float(cfg.cost_gate_max_round_trip_pct):g}%", bool(cq["cost_gate_pass"]),
                         f"slip {bd.get('slip_pct', 0):.2f} + fees {bd.get('fee_pct', 0):.2f} + protocol {bd.get('protocol_pct', 0):.2f} + shave {bd.get('shave_pct', 0):.2f} + failed-tx {bd.get('failed_pct', 0):.2f}"))

    # ---- creator verdict -------------------------------------------------------------------------------------------
    creator = b.get("creator") or (live or {}).get("creator")
    try:
        cv = await creator_verdict.compute(db, cfg, mint, creator, b)
        rows.append(_row("creator verdict", cv.get("level"), "not 'block'", cv.get("level") != "block",
                         "; ".join(cv.get("reasons") or [])[:200] + (f" · size ×{cv.get('size_mult')}" if cv.get("size_mult") not in (None, 1, 1.0) else "")))
    except Exception as e:
        rows.append(_row("creator verdict", "—", "not 'block'", None, f"unavailable: {e}"))

    # ---- history: why it entered / exited --------------------------------------------------------------------------
    trades = await db.trades.find({"mint": mint}, {"_id": 0, "id": 1, "status": 1, "mode": 1, "book": 1, "classifier_action": 1, "entry_time": 1, "exit_time": 1,
                                                  "entry_usd": 1, "pnl_usd": 1, "pnl_pct": 1, "exit_reason": 1, "entry_ctx": 1, "hold_s": 1, "cost_gate_reason": 1,
                                                  "expected_cost_pct": 1, "entry_price_sol": 1, "exit_price_sol": 1}).sort("entry_time", -1).limit(10).to_list(10)
    decisions = [{"ts": d[0], "verdict": d[1], "price_sol": d[2]} for d in (b.get("decisions") or (doc or {}).get("decisions") or [])]
    refusal = b.get("entry_refusal") if tracked is not None else None
    blockers = [r["gate"] for r in rows if r["pass"] is False]
    known = tracked is not None or bool(doc) or bool(live)
    return {
        "mint": mint, "symbol": b.get("symbol"), "protocol": proto, "band": band, "tracked": tracked is not None, "pool": pool, "known": known,
        "age_min": round(age_min, 1), "sol_usd": sol_usd, "mc_usd": round(mc), "price_sol": b.get("last_price_sol"),
        "would_enter": not blockers, "blockers": blockers, "rows": rows,
        "last_refusal": refusal, "decisions": decisions, "trades": trades, "gate_reason": b.get("gate_reason"), "gate_detail": b.get("gate_detail"),
    }
