"""Live execution gate.

This build cannot sign or send. ``refuse_live`` raises on every call, including
when ``execution_mode`` is ``live``, ``live_enabled`` is true, and
``TAILBOT_LIVE_KEY_PATH`` is set. The path is only checked for presence. The file
is not opened and no key bytes are read. Wiring a signer is a later change.
"""
from __future__ import annotations

import os


class LiveTradingBlocked(RuntimeError):
    """Raised instead of signing or sending."""


def live_block_reason(cfg) -> str:
    mode = getattr(cfg, "execution_mode", "paper")
    enabled = bool(getattr(cfg, "live_enabled", False))
    if mode != "live" or not enabled:
        return "paper mode (execution_mode is not live, or live_enabled is false)"
    key_path = os.environ.get("TAILBOT_LIVE_KEY_PATH", "").strip()
    if not key_path:
        return "live flag is on but TAILBOT_LIVE_KEY_PATH is empty; refusing before any key is loaded"
    return "live flag and key path are set, but this build has no signer and will not load the key file"


def refuse_live(cfg) -> None:
    raise LiveTradingBlocked(live_block_reason(cfg) + " — no key was loaded and no transaction was signed or sent")


def assert_paper_process(cfg) -> None:
    """Startup guard. A live config does not fall through into paper trading."""
    if getattr(cfg, "execution_mode", "paper") != "paper" or bool(getattr(cfg, "live_enabled", False)):
        refuse_live(cfg)
