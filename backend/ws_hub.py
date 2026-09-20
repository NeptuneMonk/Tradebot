"""
Lightweight WebSocket hub. Singleton.
Backend code calls `hub.broadcast(event_type, data)` and all connected
clients receive the JSON-encoded message.

Live launch path is SLIM + PATCH: the first time a launch qualifies as a candidate the wire gets one `candidate`
frame with the live fields only (`LIVE_FIELDS`, + `seq`); every later `launch_update` becomes
`candidate_update {id, seq, p: {changed fields}}`. The full launch document stays in Mongo (token detail / REST).
"""
import asyncio
import json
import logging
from fastapi import WebSocket

logger = logging.getLogger("ws_hub")

# Everything the dashboard reads off a live launch row (RecentLaunchesFeed, SkipTicker, candidate gating).
LIVE_FIELDS = frozenset((
    "id", "mint", "chain", "symbol", "name", "detected_at", "creator", "creator_eth", "creator_sol", "creator_sold_pct",
    "creator_tokens_created", "creator_tokens_graduated", "creator_tokens_failed", "creator_prior_launches",
    "creator_graduated_before", "classifier_action", "classifier_risk", "entry_action", "entered", "scanner_eligible",
    "gate", "gate_detail", "rh_gate", "rh_gate_detail", "graduated", "protocol", "quote_symbol", "quote_inflow",
    "unique_buyers", "sol_inflow", "buy_count", "curve_fill_pct", "usd_market_cap", "price_quote", "peak_mc_usd",
    "project_score", "project_flags", "project_meta_seen", "social_score", "live_pnl_pct", "live_drawdown_from_peak_pct",
    "exit_pnl_pct", "exit_reason", "pinned", "pin_strategy", "pin_exited", "bonding_curve", "dropped",
))
# Identity of a launch that is NOT a candidate yet — kept so a late qualifier's first `candidate` frame has a name.
IDENT_FIELDS = ("id", "mint", "chain", "symbol", "name", "creator", "detected_at", "quote_symbol", "protocol", "bonding_curve")
SEEN_CAP = 200
IDENT_CAP = 1500


def slim_launch(d: dict) -> dict:
    return {k: v for k, v in d.items() if k in LIVE_FIELDS}


class WSHub:
    # Raw launches run at hundreds/min; the dashboard only needs CANDIDATES (tens/min). `launch` / `launch_update`
    # become `candidate` / `candidate_update` and are dropped unless the payload (merged with what we last sent for
    # that id) is a candidate. A mint that turns into a skip gets one final `candidate_update` so the UI can drop it.
    CANDIDATE_ACTIONS = {"scalp", "hunt", "greylist_snipe", "reentry", "manual",
                         "rh_pons", "rh_pons_paper", "rh_pons_reentry", "rh_pons_manual"}
    PENDING_MIN_BUYERS = 5

    def __init__(self):
        self.clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self.mirror = None  # async (event_type, data) -> None; set by server.py when the pod singleton is wired
        self._seen: dict[str, dict] = {}    # launch id → {"seq": int, "d": slim merged payload last sent}
        self._ident: dict[str, dict] = {}   # launch id → identity stub of a not-yet-candidate launch
        self.stats = {"frames": 0, "bytes": 0}

    async def connect(self, ws: WebSocket):
        await ws.accept()
        async with self._lock:
            self.clients.add(ws)
        logger.info(f"WS connected (n={len(self.clients)})")

    async def disconnect(self, ws: WebSocket):
        async with self._lock:
            self.clients.discard(ws)
        try:
            await ws.close()
        except Exception:
            pass
        logger.info(f"WS disconnected (n={len(self.clients)})")

    @classmethod
    def is_candidate(cls, d: dict) -> bool:
        if d.get("entered") or d.get("scanner_eligible"):
            return True
        a = d.get("classifier_action")
        if a in cls.CANDIDATE_ACTIONS:
            return True
        buyers = int(d.get("unique_buyers") or 0)
        if d.get("chain") == "rh":
            # RH rows are gated by rh_paper (`rh_gate`): pass → candidate; otherwise the same "hot enough to watch"
            # tier as Solana pending rows so the RH tab is never empty while the poller is alive
            return d.get("rh_gate") == "pass" or buyers >= cls.PENDING_MIN_BUYERS
        return a == "pending" and buyers >= cls.PENDING_MIN_BUYERS

    @staticmethod
    def _prune(d: dict, cap: int):
        if len(d) > cap:
            for k in list(d)[: len(d) - cap]:
                d.pop(k, None)

    def _gate_launch(self, event_type: str, data: dict):
        lid = data.get("id")
        prev = self._seen.get(lid) if lid else None
        base = prev["d"] if prev else (self._ident.get(lid) or {})
        merged = {**base, **data}
        if not self.is_candidate(merged):
            if prev:
                self._seen.pop(lid, None)
                return "candidate_update", {"id": lid, "seq": prev["seq"] + 1, "p": {"dropped": True}}
            if lid and event_type == "launch":
                self._ident[lid] = {k: merged[k] for k in IDENT_FIELDS if k in merged}
                self._prune(self._ident, IDENT_CAP)
            return None, None
        if not prev:
            slim = slim_launch(merged)
            if lid:
                self._ident.pop(lid, None)
                self._seen[lid] = {"seq": 1, "d": slim}
                self._prune(self._seen, SEEN_CAP)
            return "candidate", {**slim, "seq": 1}
        changed = {k: v for k, v in slim_launch(data).items() if prev["d"].get(k) != v}
        if not changed:
            return None, None
        prev["d"].update(changed)
        prev["seq"] += 1
        return "candidate_update", {"id": lid, "seq": prev["seq"], "p": changed}

    async def broadcast(self, event_type: str, data, *, mirror: bool = True):
        if event_type in ("launch", "launch_update") and isinstance(data, dict):
            event_type, data = self._gate_launch(event_type, data)
            if event_type is None:
                return
        if mirror and self.mirror is not None:
            # leader → capped ws_events collection; follower pods tail it into their own sockets
            await self.mirror(event_type, data)
        if not self.clients:
            return
        msg = json.dumps({"type": event_type, "data": data}, default=str)
        self.stats["frames"] += 1
        self.stats["bytes"] += len(msg)
        dead: list[WebSocket] = []
        for ws in list(self.clients):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self.clients.discard(ws)

    @property
    def diagnostics(self) -> dict:
        return {"clients": len(self.clients), "seen": len(self._seen), "ident": len(self._ident),
                "frames": self.stats["frames"], "bytes": self.stats["bytes"],
                "avg_frame_bytes": round(self.stats["bytes"] / self.stats["frames"]) if self.stats["frames"] else 0}


hub = WSHub()
