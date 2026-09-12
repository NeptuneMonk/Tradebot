"""Why is RH not trading? One answer, every layer: run state, feed, arming, env, wallet files, breaker, poller, kill."""
from __future__ import annotations

import time

import rh_discovery
import rh_wallet


def rh_readiness(state) -> dict:
    cfg = state.config
    reasons: list[str] = []
    checks: dict[str, bool] = {}
    auto_disabled_at = getattr(state, "auto_disabled_on_restart_at", None)

    checks["bot_enabled"] = bool(getattr(cfg, "enabled", False))
    if not checks["bot_enabled"]:
        reasons.append("bot STOPPED — auto-disabled after a backend restart, press Start (set resume_on_restart=true to trade through restarts)" if auto_disabled_at
                       else "bot STOPPED — press Start")

    checks["rh_feed_enabled"] = bool(getattr(cfg, "rh_feed_enabled", True))
    if not checks["rh_feed_enabled"]:
        reasons.append("RH feed OFF (feed toggle)")

    live = bool(getattr(cfg, "rh_live_trading", False))
    checks["rh_armed"] = bool(getattr(cfg, "rh_paper_enabled", False)) or live
    if not checks["rh_armed"]:
        reasons.append("RH not armed — paper and live are both off")

    checks["rh_rpc_url_set"] = bool(rh_discovery.RH_RPC_URL)
    if not checks["rh_rpc_url_set"]:
        reasons.append("RH_RPC_URL is empty in this environment — Robinhood poller never started (copy it into the published service env; a git push does not carry .env secrets)")

    if live:
        checks["rh_wallet_files"] = rh_wallet.WALLET_PATH.exists() and rh_wallet.PASS_PATH.exists()
        if not checks["rh_wallet_files"]:
            reasons.append("RH live wallet files missing in this container (rh_wallet.json / rh_wallet.pass) — live buys cannot sign")
        checks["rh_wallet_funded_key"] = not rh_wallet.CREATED_THIS_BOOT
        if rh_wallet.CREATED_THIS_BOOT:
            reasons.append(f"RH live wallet was freshly generated on this boot ({rh_wallet.address()[:6]}…{rh_wallet.address()[-4:]}) — "
                           "the funded key is not in this container; import it (RH wallet card → import private key)")

    ld = getattr(state, "live_doctor", None)
    checks["rh_book_open"] = not (ld is not None and ld.book_paused("rh_pons"))
    if not checks["rh_book_open"]:
        reasons.append("rh_pons benched by the live-doctor breaker (LIFT to override)")

    rh = getattr(state, "rh_paper", None)
    checks["rh_kill_ok"] = not bool(getattr(rh, "live_kill_tripped", False))
    if not checks["rh_kill_ok"]:
        reasons.append("RH live kill switch tripped today")

    sing = getattr(state, "singleton", None)
    if sing is not None:
        checks["single_leader"] = not sing.two_leaders
        if sing.two_leaders:
            reasons.append("two leader pods detected — lease conflict, both fenced; check /api/pods")
        if not sing.is_leader:
            reasons.append("this pod is a follower — trading loops idle here by design (the leader runs them)")

    disc = getattr(state, "rh_discovery", None)
    poller_expected = (checks["rh_feed_enabled"] and checks["rh_rpc_url_set"] and (disc is None or disc.doctor_paused() is None)
                       and (sing is None or sing.is_leader))
    started_ago = time.time() - float(getattr(state, "process_started_ts", 0.0) or 0.0)
    if disc is not None and poller_expected and started_ago > 120:
        checks["rh_poller_alive"] = disc.alive(window_s=60.0)
        if not checks["rh_poller_alive"]:
            err = (disc.stats or {}).get("last_error") or ""
            reasons.append(f"RH poller not moving (head {disc.stats.get('head', 0)}) — last error: {err[:120] or 'none'}")

    return {
        "chain": "rh",
        "trading": not reasons,
        "reasons": reasons,
        "checks": checks,
        "mode": "live" if live else ("paper" if checks["rh_armed"] else "off"),
        "auto_disabled_on_restart_at": auto_disabled_at,
        "resumed_on_restart_at": getattr(state, "resumed_on_restart_at", None),
        "last_live_error": getattr(rh, "last_live_error", None),
        "ts": time.time(),
    }
