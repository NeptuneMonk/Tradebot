"""Creator ↔ smart-wallet affinity — learn which creators smart wallets keep buying from, so the NEXT launch by such a
creator is favoured at t=0, before any smart wallet has had time to buy.

Per creator (on the `creators` doc):  smart_mints (launches with ≥1 smart buy) · smart_hits_total · smart_manual_total ·
smart_wallets (wallet → hits) · last_smart_at.  Rate = smart launches ÷ tokens_created."""
from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger("creator_smart")
MAX_WALLETS_PER_CREATOR = 50


def record_hit(db, creator: str | None, mint: str, wallet: str, kind: str, now: float | None = None) -> None:
    """Fire-and-forget: one smart-wallet buy on one launch by this creator (caller dedupes per mint+wallet)."""
    if not creator or db is None:
        return
    now = now or time.time()
    upd = {"$addToSet": {"smart_mints": mint}, "$inc": {"smart_hits_total": 1, f"smart_wallets.{wallet}": 1},
           "$set": {"last_smart_at": now}}
    if kind == "manual":
        upd["$inc"]["smart_manual_total"] = 1
    try:
        asyncio.ensure_future(db.creators.update_one({"_id": creator}, upd, upsert=True))
    except RuntimeError:
        pass


def summary(creator_doc: dict | None) -> dict | None:
    """{launches, smart_launches, rate_pct, hits, manual_hits, recurring_wallets, last_smart_at} or None when nothing is known."""
    d = creator_doc or {}
    smart_mints = d.get("smart_mints") or []
    if not smart_mints:
        return None
    launches = max(int(d.get("tokens_created") or 0), len(smart_mints))
    wallets = d.get("smart_wallets") or {}
    recurring = sorted(((w, n) for w, n in wallets.items() if int(n) >= 2), key=lambda x: -x[1])[:5]
    return {"launches": launches, "smart_launches": len(smart_mints), "rate_pct": round(len(smart_mints) / launches * 100.0, 1),
            "hits": int(d.get("smart_hits_total") or 0), "manual_hits": int(d.get("smart_manual_total") or 0),
            "recurring_wallets": [{"wallet": w, "hits": int(n)} for w, n in recurring], "last_smart_at": d.get("last_smart_at")}


def is_magnet(s: dict | None, cfg) -> bool:
    """A creator whose launches smart wallets keep buying: ≥ min launches with a smart buy AND rate ≥ min %."""
    if not s or not bool(_cfg(cfg, "creator_smart_magnet_enabled", True)):
        return False
    return s["smart_launches"] >= int(_cfg(cfg, "creator_smart_min_launches", 2)) and s["rate_pct"] >= float(_cfg(cfg, "creator_smart_min_rate_pct", 50.0))


def magnet_reason(s: dict) -> str:
    rec = f", {len(s['recurring_wallets'])} wallet{'s' if len(s['recurring_wallets']) != 1 else ''} came back" if s["recurring_wallets"] else ""
    return f"smart magnet: smart wallets bought {s['smart_launches']} of {s['launches']} launches ({s['rate_pct']:.0f}%, {s['hits']} buys{rec})"


async def top(db, limit: int = 30) -> list[dict]:
    rows = await db.creators.find({"smart_mints.0": {"$exists": True}},
                                  {"_id": 1, "smart_mints": 1, "smart_hits_total": 1, "smart_manual_total": 1, "smart_wallets": 1,
                                   "last_smart_at": 1, "tokens_created": 1, "tokens_graduated": 1, "tokens_failed": 1, "greylist_pattern": 1,
                                   "greylist_blacklisted": 1}).sort("smart_hits_total", -1).limit(int(limit)).to_list(int(limit))
    out = []
    for d in rows:
        s = summary(d) or {}
        out.append({"creator": d["_id"], **s, "graduated": int(d.get("tokens_graduated") or 0), "failed": int(d.get("tokens_failed") or 0),
                    "pattern": d.get("greylist_pattern"), "blacklisted": bool(d.get("greylist_blacklisted"))})
    out.sort(key=lambda r: (r.get("smart_launches", 0), r.get("rate_pct", 0), r.get("hits", 0)), reverse=True)
    return out


def _cfg(cfg, key, default):
    v = getattr(cfg, key, None) if cfg is not None else None
    return default if v is None else v
