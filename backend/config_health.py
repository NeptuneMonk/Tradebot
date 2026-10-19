"""Config dead-state detector — settings that are individually valid but together mean "nothing can ever fire".

Pure function over the config (+ a few live numbers) so the UI can show the same warnings the bot acts on."""
from __future__ import annotations

from book_params import BOOKS, book_exit_view, book_size_mult
from r_sizer import size_trade

LEARN_MIN_BUY_SOL_HINT = 2.0   # above this a smart wallet's typical early buy rarely counts as a hit
GRADUATION_MC_SOL = 411.0      # pump.fun curve completes at (30+85) SOL / 279.9 M tokens → ≈ 411 SOL of market cap


def config_warnings(cfg, *, bankroll_usd: float = 0.0, manual_smart_wallets: int = 0, learned_smart_wallets: int = 0, sol_usd: float = 0.0) -> list[dict]:
    out: list[dict] = []

    def warn(key: str, msg: str, fix: str, level: str = "dead"):
        out.append({"key": key, "level": level, "msg": msg, "fix": fix})

    max_trade = float(cfg.max_trade_usd)
    min_trade = float(cfg.min_trade_usd)
    risk = float(cfg.risk_per_trade_pct)

    # --- NEW band: a USD market-cap floor above today's graduation cap can never be reached on the curve -----------
    if sol_usd > 0:
        grad_mc = GRADUATION_MC_SOL * sol_usd
        floor = float(getattr(cfg, "scanner_min_mc_usd_new", 0) or 0)
        if floor >= grad_mc:
            warn("scanner_min_mc_usd_new", f"New-band Min MC ${floor:,.0f} is above today's graduation cap ≈ ${grad_mc:,.0f} (411 SOL × ${sol_usd:,.0f}) — "
                 "no bonding-curve token can reach it; it graduates first.",
                 "Lower scanner_min_mc_usd_new (or use the SOL-price-independent 'Curve fill %' gate instead).")
        lo = float(getattr(cfg, "scanner_min_curve_fill_pct_new", 0) or 0)
        hi = float(getattr(cfg, "scanner_max_curve_fill_pct_new", 0) or 0)
        if lo > 0 and hi > 0 and lo >= hi:
            warn("scanner_curve_fill_new", f"New-band curve fill min {lo:g}% ≥ max {hi:g}% — the window is empty.", "Set min below max (e.g. 65–95).")
    if float(getattr(cfg, "band_new_max_age_min", 0) or 0) < float(getattr(cfg, "band_seasoned_min_age_min", 0) or 0) and \
            float(getattr(cfg, "scanner_min_curve_fill_pct_new", 0) or 0) >= 50:
        warn("band_new_max_age_min", f"You target late-curve tokens (fill ≥ {float(cfg.scanner_min_curve_fill_pct_new):g}%) but the New band ends at "
             f"{float(cfg.band_new_max_age_min):g} min — Seasoned is graduated pools only, so older curve tokens are in no band.",
             "Raise band_new_max_age_min (e.g. 60).", level="warn")

    # --- smart wallets -------------------------------------------------------------------------------------------
    if getattr(cfg, "smart_wallet_enabled", False):
        mode = getattr(cfg, "smart_wallet_mode", "both")
        if mode in ("hard", "both") and int(getattr(cfg, "smart_wallet_max_per_hour", 6) or 0) <= 0:
            warn("smart_wallet_max_per_hour", "Smart wallets are ON in hard mode but the hourly cap is 0 — a smart buy can never open a position.",
                 "Set smart_wallet_max_per_hour ≥ 1 (or switch smart_wallet_mode to soft).")
        if manual_smart_wallets == 0 and (learned_smart_wallets == 0 or not getattr(cfg, "smart_wallet_learn_enabled", True)):
            warn("smart_wallets", "Smart wallets are ON but there is no wallet to follow (no manual list, nothing learned/promoted).",
                 "Add a wallet in Smart Wallets or leave learning ON until a trader is promoted.", level="warn")
        min_buy = float(getattr(cfg, "smart_wallet_min_buy_sol", 0.2) or 0.0)
        if min_buy > LEARN_MIN_BUY_SOL_HINT:
            warn("smart_wallet_min_buy_sol", f"Smart buys only count at ≥ {min_buy:g} SOL — most early buys are far smaller, so hits will be rare.",
                 "Lower smart_wallet_min_buy_sol (0.2–1 SOL is typical).", level="warn")

    # --- sizing: can any book ever size an entry? -----------------------------------------------------------------
    for book in BOOKS:
        mult = book_size_mult(cfg, book)
        bx = book_exit_view(cfg, book)
        cap = max_trade if book != "rh_pons" else float(getattr(cfg, "rh_max_trade_usd", max_trade) or max_trade)
        sz = size_trade(bankroll_usd=bankroll_usd, risk_per_trade_pct=risk, sl_pct=bx["stop_loss_pct"], exit_slip_pct=1.0,
                        book_mult=mult, doctor_mult=1.0, governor_mult=1.0, min_trade_usd=min_trade, max_trade_usd=cap)
        label = {"scalp": "Scalp", "hunt": "Hunt (incl. smart-wallet picks)", "rh_pons": "RH"}[book]
        if sz["skip"] and mult > 0:
            if risk <= 0:
                warn(f"size_{book}", f"{label}: fixed stake ${cap:.2f} × book {mult:g} = ${cap * mult:.2f} is below min trade ${min_trade:.2f} — every entry is skipped at r-size.",
                     "Raise max trade $, raise the book multiplier, or lower min trade $.")
            elif bankroll_usd > 0:
                warn(f"size_{book}", f"{label}: bankroll ${bankroll_usd:,.0f} × risk {risk:g}% ÷ (SL {bx['stop_loss_pct']:g}% + slip) × book {mult:g} = ${sz['size_usd'] or 0:.2f} "
                     f"< min trade ${min_trade:.2f} — every entry is skipped at r-size.",
                     "Set risk / trade % to 0 for a fixed stake, raise risk %, or lower the stop-loss %.")
        if mult <= 0:
            warn(f"mult_{book}", f"{label}: book size multiplier is 0 — the book is switched off by size.", "Restore the multiplier in Autopilot.", level="warn")
        # no profit exit at all
        has_profit_exit = bx["target_r"] > 0 or bx["take_profit_pct"] > 0 or bx["trailing_stop_pct"] > 0 or \
            (book == "hunt" and (bx["ladder_1r_sell_pct"] > 0 or bx["ladder_2r_sell_pct"] > 0))
        if not has_profit_exit:
            warn(f"exits_{book}", f"{label}: Target R, TP %, Trail % and ladder legs are all 0 — the only exits are the stop-loss and the clock.",
                 "Set a TP %, a Target R or a Trail % on this book.", level="warn")
    return out
