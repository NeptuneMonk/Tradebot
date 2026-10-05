"""Inventory / correlation halt — the last N Solana closes were stop-outs or rugs inside the window → stop
opening Solana positions until the window rolls off, even if the daily kill switch is untouched."""
from __future__ import annotations

import time

HALT_N = 5
HALT_WINDOW_S = 90 * 60
LOSS_MARKERS = ("stop-loss", "ladder stop", "rip-cord", "ripcord", "rug", "null curve")
HUNT_SLOT_CAP = 2   # hunt (greylist/reentry) may occupy at most this many of the live Solana slots


def is_loss_exit(reason: str | None) -> bool:
    r = (reason or "").lower()
    if "profit" in r or "target" in r:
        return False   # profit rip-cord / target hits are wins
    return any(m in r for m in LOSS_MARKERS)


class InventoryHalt:
    def __init__(self):
        self._closes: list[tuple[float, bool]] = []   # (ts, was_loss_exit)
        self.halted_until: float = 0.0
        self.n, self.window_s = HALT_N, HALT_WINDOW_S
        self.lifted = 0

    def configure(self, cfg) -> None:
        self.n = max(2, int(getattr(cfg, "inventory_halt_n", HALT_N) or HALT_N))
        self.window_s = max(60, int(getattr(cfg, "inventory_halt_window_min", 90) or 90) * 60)

    def record_close(self, reason: str | None) -> bool:
        now = time.time()
        self._closes = [(ts, l) for ts, l in self._closes if now - ts <= self.window_s] + [(now, is_loss_exit(reason))]
        recent = self._closes[-self.n:]
        if len(recent) == self.n and all(l for _, l in recent):
            self.halted_until = recent[0][0] + self.window_s
            return True
        return False

    def lift(self) -> None:
        """Operator override: clear the halt now and forget the streak, so the next close starts a fresh count."""
        self.halted_until = 0.0
        self._closes.clear()
        self.lifted += 1

    def active(self) -> bool:
        return time.time() < self.halted_until

    def snapshot(self) -> dict:
        now = time.time()
        return {"halted": self.active(), "halted_until": self.halted_until if self.active() else None,
                "recent_loss_closes": sum(1 for ts, l in self._closes if l and now - ts <= self.window_s),
                "window_min": self.window_s // 60, "trigger_n": self.n, "lifted": self.lifted}
