"""Telegram alerts: targets-only mode fires on profit targets met and nothing else."""
import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from alerts import Alerts, is_target_reason  # noqa: E402


def test_target_reason_matcher():
    yes = ["take-profit +30% hit (+31.2%)", "target +2R hit (+44.0%)", "target +3R hit (+50%) [manual hold]",
           "ladder +1R: sell 40% (+22.0%)", "ladder +2R: sell 30% (+41.0%)", "runner take-profit +80% from promotion (+85.0%)",
           "runner target +4R from promotion (+90.0%)", "runner +3R: bank 25% (+60.0% from promotion), trail → 10%"]
    no = ["stop-loss hit (-12.0%)", "trailing-stop hit (peak +40.0%, now +25.0%)", "scalp clock 40s (+12.0%)", "hunt clock 600s (+8.0%)",
          "no-momentum (peak +3.1% in 20s, +1.2% after 25s)", "spike bank: sell 50% of the remainder (+35.0%)", "runner giveback 20.0% ≥ 15% (…)",
          "runner stop-loss -9.0% from promotion", "runner-exhausted: flow died (+12.0% from promotion)", "pnl stop", "hard-stop (user requested)",
          "promotion → runner: bank 50% (+45.0%)", "sold externally (wallet holds 0)", "ladder stop +5.0% hit (3.0%)"]
    assert all(is_target_reason(r) for r in yes), [r for r in yes if not is_target_reason(r)]
    assert not any(is_target_reason(r) for r in no), [r for r in no if is_target_reason(r)]


def _alerts(**cfg):
    base = dict(alerts_enabled=True, alert_trade_pnl_pct=20.0, alert_targets_only=True, alert_system_events=False, pnl_stop_flatten=True)
    st = SimpleNamespace(config=SimpleNamespace(**{**base, **cfg}), db=None)
    a = Alerts(st)
    a.chat_id = 1
    a.token = "x"
    a.sent = []

    async def fake_send(kind, text):
        a.sent.append((kind, text))
        return True
    a.send = fake_send
    a.enabled = lambda: True
    return a


def test_targets_only_filters_exits_partials_and_system_events():
    a = _alerts()

    async def run():
        await a.on_event("trade_exit", {"symbol": "A", "pnl_pct": 45.0, "exit_reason": "trailing-stop hit (peak +60.0%, now +45.0%)", "book": "hunt"})
        await a.on_event("trade_exit", {"symbol": "B", "pnl_pct": -30.0, "exit_reason": "stop-loss hit (-30.0%)", "book": "scalp"})
        await a.on_event("trade_exit", {"symbol": "C", "pnl_pct": 31.0, "exit_reason": "take-profit +30% hit (+31.0%)", "book": "scalp", "pnl_usd": 0.31})
        await a.on_event("trade_exit", {"symbol": "D", "pnl_pct": 5.0, "exit_reason": "target +1R hit (+5.0%)", "book": "hunt"})   # below ±20 but a target
        await a.on_event("trade_partial", {"symbol": "E", "reason": "ladder +1R: sell 40% (+22.0%)", "fraction": 0.4, "partial_realized_usd": 0.12})
        await a.on_event("trade_partial", {"symbol": "F", "reason": "spike bank: sell 50% of the remainder (+35.0%)", "fraction": 0.5})
        await a.on_event("kill_switch_tripped", {"reason": "daily loss"})
        await a.on_event("profit_sweep", {"amount_usd": 5, "mode": "live"})
    asyncio.run(run())
    syms = [t.split("<b>$")[1].split("</b>")[0] for _, t in a.sent]
    assert syms == ["C", "D", "E"]


def test_legacy_mode_and_system_switch():
    a = _alerts(alert_targets_only=False)

    async def run():
        await a.on_event("trade_exit", {"symbol": "A", "pnl_pct": 45.0, "exit_reason": "trailing-stop hit (peak +60.0%, now +45.0%)"})
        await a.on_event("trade_exit", {"symbol": "B", "pnl_pct": 5.0, "exit_reason": "target +1R hit (+5.0%)"})       # below floor in legacy mode
        await a.on_event("trade_partial", {"symbol": "E", "reason": "ladder +1R: sell 40% (+22.0%)", "fraction": 0.4})  # legacy never alerted partials
        await a.on_event("kill_switch_tripped", {"reason": "daily loss"})
    asyncio.run(run())
    assert [k for k, _ in a.sent] == ["trade", "kill"]
    b = _alerts(alert_system_events=True)

    async def run2():
        await b.on_event("kill_switch_tripped", {"reason": "daily loss"})
        await b.on_event("trade_exit", {"symbol": "A", "pnl_pct": 45.0, "exit_reason": "trailing-stop hit (peak +60.0%, now +45.0%)"})
    asyncio.run(run2())
    assert [k for k, _ in b.sent] == ["kill"]


def test_alert_text_has_ticker_mc_and_link():
    from alerts import mc_usd, fmt_mc
    # 2.5e-11 SOL per raw unit · 6 dec · 1e9 supply · $200 SOL → $5.0M
    assert fmt_mc(mc_usd(2.5e-11, 200.0, 6)) == "$5.00M"
    assert fmt_mc(mc_usd(2.5e-13, 200.0, 6)) == "$50.0K"
    assert mc_usd(0, 200.0) is None and fmt_mc(None) == "?"
    a = _alerts()

    async def run():
        await a.on_event("trade_exit", {"symbol": "WIF", "mint": "So11111111111111111111111111111111111111112", "pnl_pct": 31.0,
                                        "exit_reason": "take-profit +30% hit (+31.0%)", "book": "scalp", "mode": "live", "pnl_usd": 0.31,
                                        "entry_sol": 0.01, "entry_usd": 2.0, "exit_sol": 0.0131, "exit_usd": 2.62,
                                        "entry_price_sol": 5e-11, "exit_price_sol": 6.55e-11, "decimals": 6})
        await a.on_event("trade_partial", {"symbol": "WIF", "mint": "So11111111111111111111111111111111111111112", "reason": "ladder +1R: sell 40% (+22.0%)",
                                           "fraction": 0.4, "partial_realized_usd": 0.12, "entry_price_sol": 5e-11, "leg_price_sol": 6.1e-11,
                                           "sol_price": 200.0, "entry_sol_price": 200.0, "decimals": 6})
    asyncio.run(run())
    close, leg = a.sent[0][1], a.sent[1][1]
    assert "<b>$WIF</b>" in close and "MC $10.00M → $13.10M" in close and 'href="https://pump.fun/coin/So1111' in close and "in 2.00 $" in close
    assert "<b>$WIF</b>" in leg and "MC $10.00M → $12.20M" in leg and "pump.fun" in leg
