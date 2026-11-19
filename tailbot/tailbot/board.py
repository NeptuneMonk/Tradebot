"""Active-trades board and Telegram housekeeping.

The board stays at the bottom of the chat: other outgoing messages schedule a
debounced delete-and-resend, and a timer edits it in place when only prices
moved. Purge deletes the bot's own messages older than ``purge_after_minutes``
except the live board and an unanswered 24h Exit/Hold prompt.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("tailbot.board")
CHI = ZoneInfo("America/Chicago")
TELEGRAM_DELETE_MAX_S = 48 * 3600


def short_id(value: str) -> str:
    text = str(value or "")
    if len(text) <= 10:
        return text
    return f"{text[:4]}…{text[-4:]}"


def format_held(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def armed_labels(pos: dict, cfg) -> list[str]:
    """Hard stop is always on. Trail and momentum show once their arm levels are reached."""
    labels = ["stop"]
    entry = float(pos.get("entry_price") or 0)
    if entry <= 0:
        return labels
    peak = float(pos.get("peak_price") or entry)
    last = float(pos.get("last_price") or entry)
    if (peak / entry - 1.0) * 100.0 >= float(getattr(cfg, "trail_arm_pct", 50)):
        labels.append("trail")
    if (last / entry - 1.0) * 100.0 > float(getattr(cfg, "momentum_arm_pct", 20)):
        labels.append("momentum")
    return labels


def format_active_trades(positions: list[dict], *, pot: float, deployed: float, realized: float, now: float, cfg) -> str:
    pct = (deployed / pot * 100.0) if pot else 0.0
    lines = [
        "Active trades",
        f"pot {pot:.4f} SOL · deployed {pct:.0f}% · today {realized:+.4f} SOL",
    ]
    if not positions:
        lines.append("")
        lines.append("no open positions")
        return "\n".join(lines)
    for pos in positions:
        mint = str(pos.get("mint") or "")
        short = short_id(mint)
        symbol = str(pos.get("symbol") or "").strip()
        name = f"{symbol} {short}" if symbol else short
        opened = float(pos.get("opened_ts") or now)
        when = datetime.fromtimestamp(opened, CHI).strftime("%m-%d %H:%M CT")
        held = format_held(now - opened)
        entry = float(pos.get("entry_price") or 0)
        last = float(pos.get("last_price") or entry)
        decimals = int(pos.get("decimals") or 6)
        ui = int(pos.get("tokens_raw") or 0) / (10 ** decimals)
        value = last * ui
        cost = float(pos.get("cost_sol") or 0)
        pnl_sol = value - cost
        pnl_pct = (pnl_sol / cost * 100.0) if cost else 0.0
        lines.append("")
        lines.append(f"{name} · {short_id(pos.get('leader') or '')}")
        lines.append(f"in {when} · held {held}")
        lines.append(f"entry {cost:.4f} · now {value:.4f} · {pnl_pct:+.1f}% ({pnl_sol:+.4f} SOL)")
        lines.append("armed: " + " ".join(armed_labels(pos, cfg)))
    return "\n".join(lines)


class TradeBoard:
    """Mockable chat board. ``transport`` is send/edit/delete; it does not bump the board itself."""

    def __init__(self, db, transport, text_fn, now, sleep, cfg):
        self.db = db
        self.transport = transport
        self.text_fn = text_fn
        self.now = now
        self.sleep = sleep
        self.cfg = cfg
        self._board_id: int | None = None
        self._text = ""
        self._bump_after: float | None = None
        self._last_edit = 0.0
        self._next_ok = 0.0
        self._next_purge = 0.0

    def _debounce(self) -> float:
        return float(getattr(self.cfg, "board_debounce_s", 2) or 2)

    def _edit_every(self) -> float:
        return float(getattr(self.cfg, "board_edit_s", 45) or 45)

    def _min_interval(self) -> float:
        return float(getattr(self.cfg, "tg_min_interval_s", 1.1) or 1.1)

    def _purge_after(self) -> float:
        return float(getattr(self.cfg, "purge_after_minutes", 60) or 60) * 60.0

    def _purge_every(self) -> float:
        return float(getattr(self.cfg, "purge_every_s", 180) or 180)

    def schedule_bump(self) -> None:
        self._bump_after = self.now() + self._debounce()

    async def gate(self) -> None:
        """About one Telegram call per second in this chat."""
        wait = self._next_ok - self.now()
        if wait > 0:
            await self.sleep(wait)
        self._next_ok = self.now() + self._min_interval()

    async def start(self) -> None:
        """Drop the board left behind by the previous process, then post a fresh one."""
        raw = self.db.kv_get("tg_board_id")
        if raw:
            try:
                old = int(raw)
            except (TypeError, ValueError):
                old = 0
            if old:
                await self._delete_quiet(old)
                self.db.forget_tg(old)
        self.db.kv_set("tg_board_id", "")
        self._board_id = None
        await self._send_board()
        self._next_purge = self.now() + self._purge_every()

    async def tick(self) -> None:
        if self._bump_after is not None and self.now() >= self._bump_after:
            self._bump_after = None
            await self._resend()
        elif self._board_id and self.now() - self._last_edit >= self._edit_every():
            await self._edit()
        elif not self._board_id and self._bump_after is None:
            await self._send_board()
        if self.now() >= self._next_purge:
            self._next_purge = self.now() + self._purge_every()
            await self.purge()

    async def _resend(self) -> None:
        if self._board_id:
            await self._delete_quiet(self._board_id)
            self.db.forget_tg(self._board_id)
            self._board_id = None
            self.db.kv_set("tg_board_id", "")
        await self._send_board()

    async def _send_board(self) -> None:
        text = self.text_fn()
        await self.gate()
        mid = int(await self.transport.send_message(text))
        self.db.record_tg(mid, self.now(), "board", None)
        self.db.kv_set("tg_board_id", str(mid))
        self._board_id = mid
        self._text = text
        self._last_edit = self.now()

    async def _edit(self) -> None:
        text = self.text_fn()
        self._last_edit = self.now()
        if text == self._text or not self._board_id:
            return
        await self.gate()
        try:
            await self.transport.edit_message(self._board_id, text)
        except Exception as exc:
            log.info("board edit failed (%s); posting a new one", exc)
            self.db.forget_tg(self._board_id)
            self._board_id = None
            await self._send_board()
            return
        self._text = text

    async def _delete_quiet(self, message_id: int) -> None:
        await self.gate()
        try:
            await self.transport.delete_message(int(message_id))
        except Exception as exc:
            log.info("telegram delete %s ignored: %s", message_id, exc)

    def _prompt_open(self, position_id) -> bool:
        if not position_id:
            return False
        pos = self.db.position(int(position_id))
        if not pos or pos.get("status") != "open":
            return False
        return int(pos.get("prompted") or 0) == 1 and not float(pos.get("next_prompt_ts") or 0)

    async def purge(self) -> None:
        cutoff = self.now() - self._purge_after()
        horizon = self.now() - TELEGRAM_DELETE_MAX_S
        for row in self.db.tg_messages():
            mid = int(row["message_id"])
            sent = float(row["sent_at"])
            if mid == self._board_id:
                continue
            if row["kind"] == "prompt" and self._prompt_open(row.get("position_id")):
                continue
            if sent > cutoff:
                continue
            if sent <= horizon:
                # Telegram refuses deletes older than 48h. Drop the row and move on.
                self.db.forget_tg(mid)
                continue
            await self.gate()
            try:
                await self.transport.delete_message(mid)
            except Exception as exc:
                log.info("purge %s ignored: %s", mid, exc)
                text = str(exc).lower()
                if "too many" in text or "retry" in text or "429" in text:
                    return
            self.db.forget_tg(mid)
