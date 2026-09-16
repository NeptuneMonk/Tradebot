"""Universal re-entry policy — one exit memory per chain, driven by the existing `reentry_*` controls.

A token bought again inside `reentry_window_seconds` of its last exit is a RE-ENTRY on every book and venue
(Pump.fun curve, PumpSwap, RH curve, RH v4 pool), whether the trigger is the pullback/breakout watch or the
normal entry gates passing again. It needs `reentry_enabled`, fewer than `reentry_max_attempts` re-entries,
at least `reentry_min_wait_s` since the exit, and is sized × `reentry_size_multiplier`. A hot token
(last exit ≥ `hot_token_pnl_pct`) gets +2 attempts, a doubled window and × `hot_reentry_size_mult`, exactly
like the Sol watcher. Outside the window the token is a fresh candidate again.
"""
import time


class ReentryLedger:
    def __init__(self):
        self.exits: dict[str, dict] = {}

    @staticmethod
    def _window_s(e: dict, cfg) -> float:
        return float(getattr(cfg, "reentry_window_seconds", 300) or 300) * (2 if e["hot"] else 1)

    @staticmethod
    def _max_attempts(e: dict, cfg) -> int:
        return int(getattr(cfg, "reentry_max_attempts", 2) or 0) + (2 if e["hot"] else 0)

    def record_exit(self, token: str, pnl_pct: float, cfg, was_sl: bool = False, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        prev = self.exits.get(token)
        carry = bool(prev) and now - prev["ts"] <= self._window_s(prev, cfg)
        hot = (carry and prev["hot"]) or float(pnl_pct or 0) >= float(getattr(cfg, "hot_token_pnl_pct", 25.0) or 25.0)
        e = {"ts": now, "attempts": int(prev["attempts"]) if carry else 0, "hot": hot, "was_sl": bool(was_sl),
             "last_pnl_pct": float(pnl_pct or 0)}
        self.exits[token] = e
        return e

    def record_attempt(self, token: str) -> int:
        e = self.exits.get(token)
        if e is None:
            return 0
        e["attempts"] += 1
        return e["attempts"]

    def check(self, token: str, cfg, now: float | None = None) -> tuple[str | None, float | None]:
        """(block_reason, size_mult). (None, None) → fresh token, no exit inside the window.
        (None, mult) → re-entry allowed at `mult` × size. (reason, None) → refused."""
        now = time.time() if now is None else now
        e = self.exits.get(token)
        if e is None:
            return None, None
        since = now - e["ts"]
        if since > self._window_s(e, cfg):
            self.exits.pop(token, None)
            return None, None
        if not getattr(cfg, "reentry_enabled", True):
            return "reentry-off", None
        if since < float(getattr(cfg, "reentry_min_wait_s", 20) or 0):
            return "reentry-wait", None
        if e["attempts"] >= self._max_attempts(e, cfg):
            return "reentry-max", None
        mult = float(getattr(cfg, "reentry_size_multiplier", 0.5) or 0.5)
        if e["hot"]:
            mult *= float(getattr(cfg, "hot_reentry_size_mult", 1.5) or 1.0)
        return None, mult

    def attempts(self, token: str) -> int:
        e = self.exits.get(token)
        return int(e["attempts"]) if e else 0

    def prune(self, cfg, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for token, e in list(self.exits.items()):
            if now - e["ts"] > self._window_s(e, cfg):
                self.exits.pop(token, None)
