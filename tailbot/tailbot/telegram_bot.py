"""Telegram command routing. Only ``TELEGRAM_CHAT_ID`` is accepted.

The HTTP call shape (Bot API sendMessage / getUpdates, no SDK) follows
``backend/alerts.py``. Commands and the fixed chat id are tailbot's.
"""
from __future__ import annotations

import logging

import httpx

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


class Telegram:
    def __init__(self, token: str, chat_id: str, control):
        self.token = (token or "").strip()
        self.chat_id = str(chat_id or "").strip()
        self.control = control
        self.offset = 0
        self.outbox: list[str] = []

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    async def send(self, text: str, markup: dict | None = None) -> None:
        self.outbox.append(text)
        log.info("alert: %s", text.replace("\n", " | "))
        if not self.enabled:
            return
        payload = {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}
        if markup:
            payload["reply_markup"] = markup
        await self._post("sendMessage", payload)

    async def run(self) -> None:
        if not self.enabled:
            log.warning("telegram disabled (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)")
            return
        while True:
            try:
                updates = await self._post("getUpdates", {"offset": self.offset, "timeout": 25})
            except Exception as exc:
                log.warning("telegram getUpdates: %s", exc)
                import asyncio
                await asyncio.sleep(5)
                continue
            for update in updates or []:
                self.offset = int(update["update_id"]) + 1
                await self.handle_update(update)

    async def handle_update(self, update: dict) -> None:
        routed = route_update(update, self.chat_id)
        if routed is None:
            return
        kind, data = routed
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
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        async with httpx.AsyncClient(timeout=35.0) as client:
            resp = await client.post(url, json=payload)
        body = resp.json() if resp.content else {}
        if resp.status_code != 200 or not body.get("ok", True):
            raise RuntimeError(body.get("description") or f"telegram {resp.status_code}")
        return body.get("result")
