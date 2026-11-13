"""Two-tier check cadence: new accounts fast, old accounts slow (env configurable)."""
from __future__ import annotations

import os
import time
import random
import statistics
from collections import Counter, deque
from datetime import datetime

FAST_S = float(os.getenv("FAST_INTERVAL_SECONDS", 60))
SLOW_S = float(os.getenv("SLOW_INTERVAL_SECONDS", 120))
FAST_WINDOW_H = float(os.getenv("FAST_WINDOW_HOURS", 168))


def _age_hours(rec: dict) -> float:
    c = rec.get("created_at")
    if isinstance(c, str):
        try:
            c = datetime.fromisoformat(c)
        except ValueError:
            c = None
    if not isinstance(c, datetime):
        return 0.0  # unknown age -> fast tier (safe side)
    return max(0.0, (datetime.now() - c).total_seconds() / 3600)


class TierScheduler:
    def __init__(self):
        self.last_checked: dict[tuple, float] = {}
        self.outcomes: deque = deque(maxlen=5000)  # (monotonic_ts, status)
        self.lag = {"fast": deque(maxlen=500), "slow": deque(maxlen=500)}
        self.counts = {"fast": 0, "slow": 0}

    def due(self, records: list[dict]) -> list[dict]:
        now = time.monotonic()
        keys = {(r.get("guild_id"), r.get("username")) for r in records}
        for k in [k for k in self.last_checked if k not in keys]:
            del self.last_checked[k]
        out = []
        counts = {"fast": 0, "slow": 0}
        for r in records:
            k = (r.get("guild_id"), r.get("username"))
            tier = "fast" if _age_hours(r) < FAST_WINDOW_H else "slow"
            interval = FAST_S if tier == "fast" else SLOW_S
            counts[tier] += 1
            if k not in self.last_checked:
                # spread first checks randomly inside the interval
                self.last_checked[k] = now - interval + random.uniform(0.5, interval)
            overdue = now - self.last_checked[k] - interval
            if overdue >= 0:
                out.append((overdue, k, r, tier, now - self.last_checked[k]))
        self.counts = counts
        out.sort(key=lambda x: -x[0])
        for _, k, _r, tier, elapsed in out:
            self.last_checked[k] = now
            self.lag[tier].append(elapsed)
        return [x[2] for x in out]

    def record_outcome(self, status):
        self.outcomes.append((time.monotonic(), status or "none"))

    def health_text(self) -> str:
        now = time.monotonic()
        mix = Counter(st for t, st in self.outcomes if now - t <= 3600)
        med = lambda d: f"{statistics.median(d):.0f}s" if d else "-"
        return (
            "Monitor health\n"
            f"Fast tier ({FAST_S:.0f}s, under {FAST_WINDOW_H/24:.0f} days): {self.counts['fast']} accounts, real interval {med(self.lag['fast'])}\n"
            f"Slow tier ({SLOW_S:.0f}s): {self.counts['slow']} accounts, real interval {med(self.lag['slow'])}\n"
            f"Last hour: {sum(mix.values())} checks {dict(mix.most_common())}"
        )
