"""Two-tier check cadence: new accounts fast, old accounts slow (env configurable)."""
from __future__ import annotations

import asyncio
import os
import time
import random
import statistics
from collections import Counter, deque
from datetime import datetime

FAST_S = float(os.getenv("FAST_INTERVAL_SECONDS", 60))
SLOW_S = float(os.getenv("SLOW_INTERVAL_SECONDS", 120))
FAST_WINDOW_H = float(os.getenv("FAST_WINDOW_HOURS", 168))
RETRY_S = float(os.getenv("RETRY_INTERVAL_SECONDS", 8))
RETRY_MAX = int(os.getenv("RETRY_MAX_CONSECUTIVE", 1))
NAME_WATCH_S = float(os.getenv("NAME_WATCH_INTERVAL_SECONDS", 120))
# Ban confirmation: a ban alert needs several conclusive "unavailable" answers from independent requests
# (new IP each) with no conclusive "active" in between. Blocked/login-wall answers are not evidence either way,
# so they must not throw the first observation away (that made alerts take ~1h when the paid pool is weak).
BAN_CONFIRM_TRIES = int(os.getenv("BAN_CONFIRM_TRIES", 8))
BAN_CONFIRM_SIGHTINGS = int(os.getenv("BAN_CONFIRM_SIGHTINGS", 3))
BAN_CONFIRM_WINDOW_S = float(os.getenv("BAN_CONFIRM_WINDOW_SECONDS", 300))
BAN_CONFIRM_GAP_S = float(os.getenv("BAN_CONFIRM_GAP_SECONDS", 1.5))


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
        self.last_checked: dict[str, float] = {}
        self.retries: dict[str, int] = {}
        self.ban_seen: dict[str, list] = {}  # username -> times of unconfirmed conclusive 'unavailable' sightings
        self.outcomes: deque = deque(maxlen=5000)  # (monotonic_ts, status)
        self.lag = {"fast": deque(maxlen=500), "slow": deque(maxlen=500)}
        self.counts = {"fast": 0, "slow": 0}

    def due_groups(self, records: list[dict]) -> list[list[dict]]:
        """Group records by username (the same account watched in several guilds is checked ONCE per
        interval) and return the groups that are due. The youngest record decides the tier."""
        now = time.monotonic()
        groups: dict[str, list[dict]] = {}
        for r in records:
            groups.setdefault(r.get("username"), []).append(r)
        for k in [k for k in self.last_checked if k not in groups]:
            del self.last_checked[k]
        for k in [k for k in self.ban_seen if k not in groups]:
            del self.ban_seen[k]
        out = []
        counts = {"fast": 0, "slow": 0}
        for k, recs in groups.items():
            tier = "fast" if min(_age_hours(r) for r in recs) < FAST_WINDOW_H else "slow"
            interval = FAST_S if tier == "fast" else SLOW_S
            counts[tier] += 1
            if k not in self.last_checked:
                # spread first checks randomly inside the interval
                self.last_checked[k] = now - interval + random.uniform(0.5, interval)
            overdue = now - self.last_checked[k] - interval
            if overdue >= 0:
                out.append((overdue, k, recs, tier, now - self.last_checked[k]))
        self.counts = counts
        out.sort(key=lambda x: -x[0])
        for _, k, _recs, tier, elapsed in out:
            self.last_checked[k] = now
            self.lag[tier].append(elapsed)
        return [x[2] for x in out]

    def due(self, records: list[dict]) -> list[dict]:
        """Flat list of due records (kept for compatibility)."""
        return [r for g in self.due_groups(records) for r in g]

    def tier_of(self, recs) -> str:
        recs = recs if isinstance(recs, list) else [recs]
        return "fast" if min(_age_hours(r) for r in recs) < FAST_WINDOW_H else "slow"

    def paid_ok(self, recs) -> bool:
        """Paid per-GB proxy is allowed only for fast-tier accounts and only on the first try of a
        round (quick retries stay on free static IPs)."""
        recs = recs if isinstance(recs, list) else [recs]
        return self.tier_of(recs) == "fast" and self.retries.get(recs[0].get("username"), 0) == 0

    def after_check(self, rec: dict, status):
        """Inconclusive result (blocked/rate-limited/unknown): re-check in RETRY_S instead of a full interval.
        Capped at RETRY_MAX in a row so a permanently failing account cannot burn bandwidth."""
        k = (rec[0] if isinstance(rec, list) else rec).get("username")
        if status in ("active", "unavailable"):
            self.retries.pop(k, None)
            return
        n = self.retries.get(k, 0)
        if n >= RETRY_MAX:
            self.retries.pop(k, None)  # give up for this round, normal interval applies
            return
        self.retries[k] = n + 1
        interval = FAST_S if self.tier_of(rec) == "fast" else SLOW_S
        if RETRY_S < interval:
            self.last_checked[k] = time.monotonic() - interval + RETRY_S

    def note_status(self, username, status):
        """A conclusive 'active' proves the account is live: forget earlier 'unavailable' sightings."""
        if status == "active":
            self.ban_seen.pop(username, None)

    async def confirm_ban(self, checker, username, first):
        """`first` is a conclusive 'unavailable'. A ban is confirmed only after BAN_CONFIRM_SIGHTINGS conclusive
        'unavailable' answers (independent requests, new IP each) within BAN_CONFIRM_WINDOW_S with no conclusive
        'active' in between. Blocked/unknown answers are not evidence either way: they neither confirm nor erase
        earlier sightings. Returns the result to act on ('unavailable' only when confirmed)."""
        now = time.monotonic()
        seen = [t for t in self.ban_seen.get(username, []) if now - t <= BAN_CONFIRM_WINDOW_S]
        seen.append(now)
        self.ban_seen[username] = seen
        if len(seen) >= BAN_CONFIRM_SIGHTINGS:
            self.ban_seen.pop(username, None)
            return first
        last = first
        for i in range(BAN_CONFIRM_TRIES):
            if i:
                await asyncio.sleep(BAN_CONFIRM_GAP_S)
            again = await checker.check(username, allow_paid=True, fast=True)
            st = getattr(again, "status", None)
            if st == "active":
                self.ban_seen.pop(username, None)
                return again
            if st == "unavailable":
                seen = [t for t in self.ban_seen.get(username, []) if time.monotonic() - t <= BAN_CONFIRM_WINDOW_S]
                seen.append(time.monotonic())
                self.ban_seen[username] = seen
                if len(seen) >= BAN_CONFIRM_SIGHTINGS:
                    self.ban_seen.pop(username, None)
                    return again
            last = again
        return last

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
