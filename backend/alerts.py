"""
Phone alerts over Telegram (Bot API over plain HTTPS — no SDK).

Events (operator choice 2026-10-07): kill switch / PnL stop tripped, profit sweep done, trade closed with |PnL| ≥
`alert_trade_pnl_pct`, Live-Doctor book breaker tripped, SOL / RH feed down > FEED_DOWN_S (and restored).
Hub events are observed through `hub.listeners`; breakers and feeds are polled. The chat id is discovered from the
bot's `getUpdates` after the operator sends the bot any message, then persisted in `alerts` ({_id: "telegram"}).
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import time
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from bot import BotState

logger = logging.getLogger("alerts")

TG_API = "https://api.telegram.org/bot{token}/{method}"
HTTP_TIMEOUT = 10.0
POLL_S = 30.0
FEED_DOWN_S = 120.0
DEDUPE_S = {"trade": 0.0, "kill": 60.0, "sweep": 0.0, "breaker": 300.0, "feed": 600.0, "test": 0.0}


class Alerts:
    def __init__(self, state: "BotState"):
        self.state = state
        self.token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        self.chat_id: int | None = None
        self.chat_title: str | None = None
        self.stats = {"sent": 0, "failed": 0, "last_sent_ts": 0.0, "last_error": "", "last_text": ""}
        self._last_kind: dict[str, float] = {}
        self._feed_down_since: dict[str, float] = {}
        self._feed_alerted: set[str] = set()
        self._paused_books: set[str] = set()
        self._task: asyncio.Task | None = None

    # ---------- wiring ----------
    def configured(self) -> bool:
        return bool(self.token)

    def enabled(self) -> bool:
        return self.configured() and self.chat_id is not None and bool(getattr(self.state.config, "alerts_enabled", True))

    async def load(self) -> None:
        try:
            doc = await self.state.db.alerts.find_one({"_id": "telegram"}, {"_id": 0})
        except Exception:
            doc = None
        if doc and doc.get("chat_id") is not None:
            self.chat_id = int(doc["chat_id"])
            self.chat_title = doc.get("chat_title")

    def start(self) -> None:
        if self.configured() and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._loop())

    def snapshot(self) -> dict:
        return {"configured": self.configured(), "connected": self.chat_id is not None, "chat_id": self.chat_id, "chat_title": self.chat_title,
                "enabled": self.enabled(), "min_trade_pnl_pct": float(getattr(self.state.config, "alert_trade_pnl_pct", 20.0) or 20.0),
                "events": ["kill switch / PnL stop", "profit sweep", f"trade closed ≥ ±{float(getattr(self.state.config, 'alert_trade_pnl_pct', 20.0) or 20.0):g} %",
                           "Doctor book breaker", "feed down > 2 min"], **self.stats}

    # ---------- Telegram ----------
    async def _call(self, method: str, **params) -> dict:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.post(TG_API.format(token=self.token, method=method), json=params)
            body = r.json() if r.content else {}
            if r.status_code != 200 or not body.get("ok"):
                raise RuntimeError(body.get("description") or f"HTTP {r.status_code}")
            return body.get("result") or {}

    async def connect(self) -> dict:
        """Read the newest private message sent to the bot → that chat becomes the alert target."""
        if not self.configured():
            return {"ok": False, "reason": "TELEGRAM_BOT_TOKEN not set"}
        try:
            updates = await self._call("getUpdates", limit=100, timeout=0)
        except Exception as e:
            self.stats["last_error"] = str(e)
            return {"ok": False, "reason": f"getUpdates failed: {e}"}
        chat = None
        for u in reversed(updates if isinstance(updates, list) else []):
            msg = u.get("message") or u.get("edited_message") or u.get("channel_post") or {}
            if msg.get("chat", {}).get("id") is not None:
                chat = msg["chat"]
                break
        if not chat:
            return {"ok": False, "reason": "no message found — open the bot in Telegram, press Start (or send any text), then connect again"}
        self.chat_id = int(chat["id"])
        self.chat_title = chat.get("title") or " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) or chat.get("username")
        await self.state.db.alerts.update_one({"_id": "telegram"}, {"$set": {"chat_id": self.chat_id, "chat_title": self.chat_title, "connected_at": time.time()}}, upsert=True)
        await self.send("test", "✅ <b>PUMP.BOT alerts connected</b>\nYou will get: kill switch / PnL stop · profit sweeps · trades closed beyond ±"
                        f"{float(getattr(self.state.config, 'alert_trade_pnl_pct', 20.0) or 20.0):g} % · Doctor breakers · feed drops.")
        return {"ok": True, **self.snapshot()}

    async def send(self, kind: str, text: str) -> bool:
        if not self.enabled():
            return False
        now = time.time()
        gap = DEDUPE_S.get(kind, 30.0)
        if gap and now - self._last_kind.get(kind, 0.0) < gap:
            return False
        self._last_kind[kind] = now
        try:
            await self._call("sendMessage", chat_id=self.chat_id, text=text, parse_mode="HTML", disable_web_page_preview=True)
            self.stats["sent"] += 1
            self.stats["last_sent_ts"] = now
            self.stats["last_text"] = text[:200]
            return True
        except Exception as e:
            self.stats["failed"] += 1
            self.stats["last_error"] = str(e)
            logger.warning(f"telegram send failed ({kind}): {e}")
            return False

    # ---------- event routing ----------
    async def on_event(self, ev: str, data) -> None:
        if not self.enabled() or not isinstance(data, dict):
            return
        cfg = self.state.config
        if ev == "pnl_stop_tripped":
            await self.send("kill", f"🛑 <b>PnL STOP TRIPPED</b> ({html.escape(str(data.get('mode', '')))})\n"
                                    f"Session PnL {float(data.get('pnl_usd') or 0):+.2f} $ ≤ −{float(data.get('limit_usd') or 0):.2f} $\n"
                                    f"Bot disabled{' · open positions flattened' if getattr(cfg, 'pnl_stop_flatten', True) else ''}.")
        elif ev == "kill_switch_tripped":
            await self.send("kill", f"🛑 <b>KILL SWITCH</b>\n{html.escape(str(data.get('reason') or ''))}\nBot disabled.")
        elif ev == "profit_sweep":
            await self.send("sweep", f"💰 <b>PROFIT SWEEP</b> ({html.escape(str(data.get('mode', '')))})\n"
                                     f"{float(data.get('amount_usd') or 0):.2f} $ → cold wallet\n"
                                     f"<code>{html.escape(str(data.get('sig') or '')[:24])}…</code>")
        elif ev == "trade_exit":
            pnl = data.get("pnl_pct")
            floor = float(getattr(cfg, "alert_trade_pnl_pct", 20.0) or 20.0)
            if pnl is None or abs(float(pnl)) < floor:
                return
            pnl = float(pnl)
            usd = data.get("pnl_usd")
            await self.send("trade", f"{'🟢' if pnl >= 0 else '🔴'} <b>{html.escape(str(data.get('symbol') or data.get('mint', '')[:8]))}</b> closed "
                                     f"<b>{pnl:+.1f} %</b>{f' ({float(usd):+.2f} $)' if usd is not None else ''}\n"
                                     f"{html.escape(str(data.get('book') or ''))} · {html.escape(str(data.get('mode') or ''))} · "
                                     f"{html.escape(str(data.get('exit_reason') or '')[:120])}")

    async def _loop(self) -> None:
        await asyncio.sleep(POLL_S)
        while True:
            try:
                await self._poll_breakers()
                await self._poll_feeds()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"alerts poll failed: {e}")
            await asyncio.sleep(POLL_S)

    async def _poll_breakers(self) -> None:
        ld = getattr(self.state, "live_doctor", None)
        if ld is None or not getattr(self.state.config, "live_doctor_breakers_enabled", True):
            return
        paused = set(ld.book_paused_until().keys())
        for book in sorted(paused - self._paused_books):
            why = (getattr(ld, "last_book_breakers", {}).get(book) or {}).get("reason") or "book paused"
            await self.send("breaker", f"⚠️ <b>DOCTOR BREAKER</b> paused <b>{html.escape(book)}</b>\n{html.escape(str(why)[:200])}")
        self._paused_books = paused

    async def _poll_feeds(self) -> None:
        st = self.state
        now = time.time()
        feeds = {}
        if getattr(st.config, "helius_tracker_enabled", True):
            feeds["SOL Pump.fun"] = bool(getattr(st, "listener_connected", False))
        rh = getattr(st, "rh_discovery", None)
        if rh is not None and getattr(st.config, "rh_feed_enabled", False):
            feeds["RH"] = rh.alive(window_s=90.0)
        for name, up in feeds.items():
            if up:
                if name in self._feed_alerted:
                    self._feed_alerted.discard(name)
                    await self.send("feed", f"✅ <b>{html.escape(name)} feed restored</b>")
                self._feed_down_since.pop(name, None)
                continue
            since = self._feed_down_since.setdefault(name, now)
            if now - since >= FEED_DOWN_S and name not in self._feed_alerted:
                self._feed_alerted.add(name)
                await self.send("feed", f"📡 <b>{html.escape(name)} feed DOWN</b> for {int((now - since) / 60)} min — no new launches are being seen.")
