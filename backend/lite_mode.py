"""Lite-mode watchdog (cut-the-fat Phase 3). On a 512Mi / 250m pod the platform OOM-kills the whole bot when RAM
spikes; the watchdog degrades gracefully first. Trips when RSS > `lite_mode_rss_mb` or 1-minute avg event-loop lag >
`lite_mode_lag_ms`; clears with hysteresis (RSS < 85% of the threshold and lag < half, for 60 s). While active:
no metadata fetches, Doctor cycle skipped, seasoned per-token refresh skipped, launch persistence off, tracker cap
cut to LITE_TRACKED_MINTS. Trading loops (listener, scanner, monitors, exits, WS) never pause."""
import logging
import resource
import sys
import time

logger = logging.getLogger(__name__)

LITE_TRACKED_MINTS = 60
CLEAR_AFTER_S = 60.0


def rss_mb() -> float:
    ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return ru / (1024.0 * 1024.0) if sys.platform == "darwin" else ru / 1024.0


def current_rss_mb() -> float:
    """Live RSS from /proc (ru_maxrss is a high-water mark and never comes down)."""
    try:
        with open("/proc/self/statm") as f:
            pages = int(f.read().split()[1])
        return pages * resource.getpagesize() / (1024.0 * 1024.0)
    except Exception:
        return rss_mb()


class LiteMode:
    def __init__(self):
        self.active = False
        self.since: float | None = None
        self.reason: str | None = None
        self.rss_mb = 0.0
        self.lag_ms = 0.0
        self.trips = 0
        self._calm_since: float | None = None
        self.forced: bool | None = None       # operator override: True = always lite, False = never, None = auto

    def evaluate(self, cfg, lag_avg_ms: float) -> bool:
        self.rss_mb = round(current_rss_mb(), 1)
        self.lag_ms = round(float(lag_avg_ms or 0.0), 1)
        if self.forced is not None:
            self._set(self.forced, "operator override")
            return self.active
        if not getattr(cfg, "lite_mode_enabled", True):
            self._set(False, None)
            return False
        rss_lim = float(getattr(cfg, "lite_mode_rss_mb", 400) or 400)
        lag_lim = float(getattr(cfg, "lite_mode_lag_ms", 500) or 500)
        now = time.time()
        hot = []
        if self.rss_mb > rss_lim:
            hot.append(f"RAM {self.rss_mb:.0f}MB > {rss_lim:.0f}MB")
        if self.lag_ms > lag_lim:
            hot.append(f"loop lag {self.lag_ms:.0f}ms > {lag_lim:.0f}ms")
        if hot:
            self._calm_since = None
            self._set(True, " · ".join(hot))
        elif self.active:
            calm = self.rss_mb < rss_lim * 0.85 and self.lag_ms < lag_lim / 2
            if not calm:
                self._calm_since = None
            elif self._calm_since is None:
                self._calm_since = now
            elif now - self._calm_since >= CLEAR_AFTER_S:
                self._set(False, None)
        return self.active

    def _set(self, on: bool, reason: str | None):
        if on and not self.active:
            self.active, self.since, self.trips = True, time.time(), self.trips + 1
            logger.warning(f"LITE MODE ON — {reason}: pausing metadata fetches, Doctor cycle, seasoned refresh, launch persistence")
        elif not on and self.active:
            logger.warning(f"LITE MODE OFF after {int(time.time() - (self.since or time.time()))}s — resources back under the line")
            self.active, self.since = False, None
        self.reason = reason if on else None

    def snapshot(self) -> dict:
        return {"active": self.active, "since": self.since, "reason": self.reason, "rss_mb": self.rss_mb, "lag_ms": self.lag_ms,
                "trips": self.trips, "forced": self.forced}
