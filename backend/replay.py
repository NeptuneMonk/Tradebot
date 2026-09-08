"""Universe replay — counterfactual entry-gate optimizer over EVERY tracked token's tick path (tick_store),
not just the ones we filled. Simulates "would this gate set have entered, and what would the book's exit
ladder have made" for the current config and one-parameter variants, so the Doctor sees missed winners,
dodged rugs, and the measured $ effect of loosening/tightening each gate on the full universe.
"""
from __future__ import annotations

import time

from book_params import book_exit_view
from rh_paper import fee_fraction

GATE_GRID = {
    "rh_pons": {"rh_min_growth_pct": [10, 20, 30, 45, 60, 80], "rh_max_growth_pct": [60, 80, 100, 150, 200, 400],
                "rh_min_unique_buyers": [3, 5, 8, 12, 16, 20], "rh_min_inflow_usd": [200, 500, 1000, 1500, 2000, 3000],
                "rh_min_mc_usd": [2000, 5000, 8000, 12000, 20000, 30000]},
    "momentum": {"min_buyers_for_entry": [3, 5, 8, 12, 16, 20]},
}
CHAIN_OF = {"rh_pons": "rh", "momentum": "sol"}
FEE_IN = {"rh": 0.01, "sol": 0.01}
FEE_OUT = {"rh": 0.01, "sol": 0.01}
IMPACT = {"rh": 0.01, "sol": 0.01}          # own price impact / slippage per side on a thin micro-cap curve
GAS_SIDE_USD = {"rh": 0.09, "sol": 0.05}
SUPPLY = 1_000_000_000
MAX_SAMPLES_PER_TOKEN = 400
MIN_GAIN_TOTAL_USD = 0.5


def _g(cfg, key, default=None):
    v = cfg.get(key) if isinstance(cfg, dict) else getattr(cfg, key, None)
    return default if v is None else v


class _Token:
    """Pre-computed per-sample features + memoised exit outcomes for one tick path."""

    def __init__(self, doc: dict, cfg, quote_usd: float, chain: str):
        self.mint, self.symbol, self.chain = doc.get("mint"), doc.get("symbol"), chain
        samples = sorted(doc.get("samples") or [], key=lambda s: s[0])
        if len(samples) > MAX_SAMPLES_PER_TOKEN:
            step = len(samples) / MAX_SAMPLES_PER_TOKEN
            samples = [samples[int(i * step)] for i in range(MAX_SAMPLES_PER_TOKEN)]
        self.ts = [s[0] for s in samples]
        self.px = [float(s[1]) for s in samples]
        buys = sorted(doc.get("buys") or [], key=lambda b: b[0])
        start = float(doc.get("start") or (self.ts[0] if self.ts else 0))
        first = float(doc.get("first_price") or (self.px[0] if self.px else 0))
        win = float(_g(cfg, "scanner_recent_inflow_window_s", 60))
        self.feat = []
        seen: set = set()
        bi = 0
        for i, t in enumerate(self.ts):
            while bi < len(buys) and buys[bi][0] <= t:
                seen.add(buys[bi][2])
                bi += 1
            recent = [b for b in buys[max(0, bi - 400):bi] if b[0] > t - win]
            new1m = {b[2] for b in buys[max(0, bi - 400):bi] if b[0] > t - 60}
            px = self.px[i]
            self.feat.append({"age": t - start, "growth": (px / first - 1.0) * 100.0 if first > 0 else 0.0,
                              "buyers": len(seen), "inflow_usd": sum(b[1] for b in recent) * quote_usd,
                              "new1m": len(new1m), "mc_usd": px * SUPPLY * quote_usd})
        self._outcome: dict[int, dict] = {}

    def passes(self, i: int, gates: dict) -> bool:
        f = self.feat[i]
        if self.chain == "rh":
            return (gates["rh_min_age_s"] <= f["age"] <= gates["rh_max_age_min"] * 60 and gates["rh_min_mc_usd"] <= f["mc_usd"] <= gates["rh_max_mc_usd"]
                    and f["buyers"] >= gates["rh_min_unique_buyers"] and gates["rh_min_growth_pct"] <= f["growth"] < gates["rh_max_growth_pct"]
                    and f["new1m"] >= gates["rh_min_new_buyers_1m"] and f["inflow_usd"] >= gates["rh_min_inflow_usd"])
        return f["age"] >= 10 and f["buyers"] >= gates["min_buyers_for_entry"]

    def first_entry(self, gates: dict) -> int | None:
        for i in range(len(self.ts)):
            if self.passes(i, gates):
                return i
        return None

    def outcome(self, i: int, ladder: dict, stake: float) -> dict:
        """Entry one sample AFTER the gate passes (latency), stops filled at the next OBSERVED price (gap-aware —
        a rug that prints −60% costs −60%, not −SL), TP filled at the TP level, RH snipe tax by token age."""
        if i in self._outcome:
            return self._outcome[i]
        k = min(i + 1, len(self.ts) - 1)
        e = self.px[k] * (1.0 + IMPACT[self.chain])
        tp, sl = ladder["take_profit_pct"] / 100.0, ladder["stop_loss_pct"] / 100.0
        arm, trail, hold = ladder["trailing_arm_pct"] / 100.0, ladder["trailing_stop_pct"] / 100.0, ladder["hold_max_seconds"]
        peak, ret, reason = e, None, "open"
        for j in range(k + 1, len(self.ts)):
            p = self.px[j]
            peak = max(peak, p)
            r = p / e - 1.0
            if r >= tp:
                ret, reason = tp, "tp"
                break
            if r <= -sl:
                ret, reason = r, "sl"                       # gap-aware: whatever the next print was
                break
            if peak / e - 1.0 >= arm and (peak - p) / peak >= trail:
                ret, reason = r, "trail"
                break
            if self.ts[j] - self.ts[k] >= hold:
                ret, reason = r, "hold"
                break
        if ret is None:
            ret = self.px[-1] / e - 1.0
        fee_in = fee_fraction(self.feat[k]["age"]) if self.chain == "rh" else FEE_IN[self.chain]
        gas = GAS_SIDE_USD[self.chain]
        proceeds = stake * (1 - fee_in) * (1 + ret) * (1 - IMPACT[self.chain]) * (1 - FEE_OUT[self.chain]) - gas
        out = {"pnl_usd": proceeds - stake - gas, "ret_pct": ret * 100.0, "reason": reason, "mfe_pct": (peak / e - 1.0) * 100.0}
        self._outcome[i] = out
        return out


def _gates_from(cfg, chain: str) -> dict:
    if chain == "rh":
        keys = ("rh_min_age_s", "rh_max_age_min", "rh_min_mc_usd", "rh_max_mc_usd", "rh_min_unique_buyers", "rh_min_growth_pct",
                "rh_max_growth_pct", "rh_min_new_buyers_1m", "rh_min_inflow_usd")
        d = {"rh_min_age_s": 10, "rh_max_age_min": 30, "rh_min_mc_usd": 0, "rh_max_mc_usd": 1e12, "rh_min_unique_buyers": 0,
             "rh_min_growth_pct": 0, "rh_max_growth_pct": 400, "rh_min_new_buyers_1m": 0, "rh_min_inflow_usd": 0}
    else:
        keys, d = ("min_buyers_for_entry",), {"min_buyers_for_entry": 0}
    return {k: float(_g(cfg, k, d[k])) for k in keys}


def _run(tokens: list[_Token], gates: dict, ladder: dict, stake: float) -> dict:
    fills, total, wins, per = 0, 0.0, 0, {}
    for tk in tokens:
        i = tk.first_entry(gates)
        if i is None:
            continue
        o = tk.outcome(i, ladder, stake)
        fills += 1
        total += o["pnl_usd"]
        wins += o["pnl_usd"] > 0
        per[tk.mint] = o
    return {"fills": fills, "total_usd": round(total, 4), "expectancy_usd": round(total / fills, 4) if fills else None,
            "winrate": round(wins / fills, 3) if fills else None, "per": per}


def _calibration(tokens: list, trades: list[dict], ladder: dict, stake: float) -> dict | None:
    """Honesty check: replay OUR real fills (entry at the sample nearest the real entry time) and compare
    the simulated $/fill with what we actually made on the same tokens."""
    by_mint = {t.mint: t for t in tokens}
    sim, real = [], []
    for tr in trades:
        tk = by_mint.get(tr.get("mint"))
        if tk is None or tr.get("pnl_usd") is None or not tr.get("entry_time"):
            continue
        try:
            from autopsy import _ts
            et = _ts(tr["entry_time"]).timestamp()
        except Exception:
            continue
        idx = next((i for i, ts in enumerate(tk.ts) if ts >= et), None)
        if idx is None or idx >= len(tk.ts) - 1:
            continue
        sim.append(tk.outcome(max(0, idx - 1), ladder, stake)["pnl_usd"])
        real.append(float(tr["pnl_usd"]))
    if len(sim) < 3:
        return None
    s, r = sum(sim) / len(sim), sum(real) / len(real)
    return {"n": len(sim), "sim_usd_per_fill": round(s, 4), "real_usd_per_fill": round(r, 4), "gap_usd": round(s - r, 4),
            "trusted": (s - r) <= max(0.5, 0.05 * stake)}


def replay_book(book: str, docs: list[dict], cfg, quote_usd: dict, min_n: int, window_h: float, real_trades: list[dict] | None = None) -> dict:
    chain = CHAIN_OF[book]
    stake = float(_g(cfg, "rh_max_trade_usd" if chain == "rh" else "max_trade_usd", 10.0) or 10.0)
    tokens = [_Token(d, cfg, float(quote_usd.get(d.get("quote_symbol") or "", 0.0) or 0.0), chain)
              for d in docs if len(d.get("samples") or []) >= 5]
    tokens = [t for t in tokens if t.feat and t.feat[0]["mc_usd"] > 0]
    ladder = book_exit_view(cfg, book)
    base_g = _gates_from(cfg, chain)
    base = _run(tokens, base_g, ladder, stake)
    rows, best = [], None
    entered_any: dict[str, dict] = {}
    for param, values in GATE_GRID[book].items():
        for v in values:
            if float(v) == base_g.get(param):
                continue
            r = _run(tokens, {**base_g, param: float(v)}, ladder, stake)
            for m, o in r["per"].items():
                if m not in base["per"]:
                    prev = entered_any.get(m)
                    if prev is None or o["pnl_usd"] > prev["pnl_usd"]:
                        entered_any[m] = {**o, "via": f"{param}={v:g}"}
            row = {"param": param, "value": v, "fills": r["fills"], "expectancy_usd": r["expectancy_usd"], "total_usd": r["total_usd"],
                   "winrate": r["winrate"], "gain_total_usd": round(r["total_usd"] - base["total_usd"], 4)}
            rows.append(row)
            if r["fills"] >= min_n and (r["expectancy_usd"] or 0) > 0 and (best is None or row["gain_total_usd"] > best["gain_total_usd"]):
                best = row
    rows.sort(key=lambda r: -r["gain_total_usd"])
    by_mint = {t.mint: t for t in tokens}
    missed = sorted(({"mint": m, "symbol": by_mint[m].symbol, "pnl_usd": round(o["pnl_usd"], 4), "mfe_pct": round(o["mfe_pct"], 1), "via": o["via"]}
                     for m, o in entered_any.items() if o["pnl_usd"] > 0 and m in by_mint), key=lambda r: -r["pnl_usd"])
    dodged = [m for m, o in entered_any.items() if o["pnl_usd"] <= -0.3 * stake]
    out = {"book": book, "chain": chain, "n_tokens": len(tokens), "window_h": window_h, "stake_usd": stake, "ladder": ladder,
           "current": {k: base[k] for k in ("fills", "total_usd", "expectancy_usd", "winrate")}, "gates": base_g,
           "rows": rows[:12], "best": None, "missed_winners": missed[:5], "missed_winners_n": len(missed),
           "missed_winners_usd": round(sum(r["pnl_usd"] for r in missed), 2), "dodged_rugs_n": len(dodged), "proposal": None,
           "calibration": _calibration(tokens, real_trades or [], ladder, stake)}
    cal = out["calibration"]
    if cal and not cal["trusted"]:
        out["note"] = (f"replay is {cal['gap_usd']:+.2f} $/fill rosier than our {cal['n']} real fills on the same tokens — "
                       f"proposals withheld until the simulator matches reality")
        return out
    if best and best["gain_total_usd"] >= max(MIN_GAIN_TOTAL_USD, 0.05 * abs(base["total_usd"])):
        out["best"] = best
        cur = base_g[best["param"]]
        out["proposal"] = {"key": best["param"], "value": best["value"], "gain": best["gain_total_usd"] / max(1, base["fills"]),
                           "reason": (f"replaying {len(tokens)} {chain.upper()} tokens tracked in the last {window_h:g}h: {best['param']} {cur:g} → {best['value']:g} "
                                      f"turns {base['fills']} fills / {base['total_usd']:+.2f} $ into {best['fills']} fills / {best['total_usd']:+.2f} $ "
                                      f"({best['expectancy_usd']:+.4f} $/fill) — measured on every token, not just the ones we bought"),
                           "evidence": {"n_tokens": len(tokens), "current": out["current"], "whatif": {k: best[k] for k in ("fills", "total_usd", "expectancy_usd")},
                                        "missed_winners_n": len(missed), "dodged_rugs_n": len(dodged)}}
    return out


async def replay_universe(tick_store, cfg, quote_usd: dict, min_n: int, window_h: float = 24.0, limit: int = 600,
                          trades_by_book: dict | None = None) -> dict:
    """{book: replay_book(...)} for every book with tick data; skips books with no paths."""
    out = {}
    since = time.time() - window_h * 3600
    for book, chain in CHAIN_OF.items():
        try:
            docs = await tick_store.load(chain, since, limit)
        except Exception:
            docs = []
        if not docs:
            continue
        out[book] = replay_book(book, docs, cfg, quote_usd, min_n, window_h, (trades_by_book or {}).get(book))
    return out
