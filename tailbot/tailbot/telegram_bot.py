"""Telegram command routing. Only ``TELEGRAM_CHAT_ID`` is accepted.

The HTTP call shape (Bot API sendMessage / getUpdates, no SDK) follows
``backend/alerts.py``. Commands and the fixed chat id are tailbot's.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from .board import TradeBoard

log = logging.getLogger("tailbot.telegram")


def parse_command(text: str) -> tuple[str, list[str]] | None:
    if not text:
        return None
    parts = text.strip().split()
    if not parts or not parts[0].startswith("/"):
        return None
    return parts[0].split("@", 1)[0].lower(), parts[1:]


def route_update(update: dict, allowed_chat: str) -> tuple[str, str] | None:
    """``('command', text)`` or ``('callback', data)``. Other chats are ignored."""
    if not str(allowed_chat or "").strip():
        return None
    allowed = str(allowed_chat).strip()
    if "callback_query" in update:
        query = update["callback_query"]
        chat = str(((query.get("message") or {}).get("chat") or {}).get("id", ""))
        if chat != allowed:
            return None
        return ("callback", str(query.get("data") or ""))
    msg = update.get("message") or update.get("edited_message") or {}
    chat = str((msg.get("chat") or {}).get("id", ""))
    if chat != allowed:
        return None
    text = msg.get("text") or ""
    if not text.startswith("/"):
        return None
    return ("command", text)


def command_context(update: dict) -> dict:
    """The command's own message id and, for a reply, the message it points at."""
    msg = update.get("message") or update.get("edited_message") or {}
    reply = msg.get("reply_to_message") or {}
    return {"message_id": msg.get("message_id"), "reply_to": reply.get("message_id")}


class Telegram:
    def __init__(self, token: str, chat_id: str, control, poster=None):
        self.token = (token or "").strip()
        self.chat_id = str(chat_id or "").strip()
        self.control = control
        self.offset = 0
        self.outbox: list[str] = []
        self._poster = poster
        self.board: TradeBoard | None = None
        if hasattr(control, "active_trades_text") and hasattr(control, "db"):
            self.board = TradeBoard(
                control.db, self, control.active_trades_text,
                control.now, control.sleep, control.cfg,
            )

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    async def send(self, text: str, markup: dict | None = None, kind: str = "alert", position_id: int | None = None):
        self.outbox.append(text)
        log.info("alert: %s", text.replace("\n", " | "))
        if not self.enabled:
            return None
        if self.board:
            await self.board.gate()
        result = await self._post("sendMessage", self._payload(text, markup))
        mid = int((result or {}).get("message_id") or 0)
        if mid and self.board:
            self.control.db.record_tg(mid, self.board.now(), kind, position_id)
            if kind != "board":
                self.board.schedule_bump()
        return mid or None

    async def send_message(self, text: str, markup: dict | None = None) -> int:
        result = await self._post("sendMessage", self._payload(text, markup))
        return int((result or {}).get("message_id") or 0)

    async def edit_message(self, message_id: int, text: str, markup: dict | None = None) -> None:
        payload = self._payload(text, markup)
        payload["message_id"] = int(message_id)
        await self._post("editMessageText", payload)

    async def delete_message(self, message_id: int) -> None:
        await self._post("deleteMessage", {"chat_id": self.chat_id, "message_id": int(message_id)})

    def _payload(self, text: str, markup: dict | None) -> dict:
        payload = {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}
        if markup:
            payload["reply_markup"] = markup
        return payload

    async def run(self) -> None:
        if not self.enabled:
            log.warning("telegram disabled (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)")
            return
        if self.board:
            try:
                await self.board.start()
            except Exception:
                log.exception("active trades board failed to start")
            asyncio.create_task(self._board_loop())
        while True:
            try:
                updates = await self._post("getUpdates", {"offset": self.offset, "timeout": 25})
            except Exception as exc:
                log.warning("telegram getUpdates: %s", exc)
                await asyncio.sleep(5)
                continue
            for update in updates or []:
                self.offset = int(update["update_id"]) + 1
                await self.handle_update(update)

    async def _board_loop(self) -> None:
        while True:
            try:
                await self.board.tick()
            except Exception:
                log.exception("board tick failed")
            await asyncio.sleep(5)

    async def handle_update(self, update: dict) -> None:
        routed = route_update(update, self.chat_id)
        if routed is None:
            return
        kind, data = routed
        if kind == "command" and parse_command(data) and parse_command(data)[0] == "/purge":
            ctx = command_context(update)
            if not ctx["reply_to"]:
                await self.send("reply to a message with /purge")
                return
            if self.board:
                await self.board.purge_reply(int(ctx["reply_to"]), int(ctx["message_id"] or 0))
            return
        if kind == "command":
            reply = await self.control.on_command(data)
        else:
            reply = await self.control.on_callback(data)
            query_id = (update.get("callback_query") or {}).get("id")
            if query_id:
                try:
                    await self._post("answerCallbackQuery", {"callback_query_id": query_id, "text": (reply or "")[:180]})
                except Exception:
                    log.debug("answerCallbackQuery failed", exc_info=True)
        if reply:
            await self.send(reply)

    async def _post(self, method: str, payload: dict):
        if self._poster is not None:
            return await self._poster(method, payload)
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        async with httpx.AsyncClient(timeout=35.0) as client:
            resp = await client.post(url, json=payload)
        body = resp.json() if resp.content else {}
        if resp.status_code != 200 or not body.get("ok", True):
            raise RuntimeError(body.get("description") or f"telegram {resp.status_code}")
        return body.get("result")
