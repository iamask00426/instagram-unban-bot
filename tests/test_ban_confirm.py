import asyncio
import types

import scheduling
from scheduling import TierScheduler


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t


class Checker:
    """Returns the scripted statuses one by one (last one repeats)."""
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = 0

    async def check(self, username, **kw):
        i = min(self.calls, len(self.statuses) - 1)
        self.calls += 1
        return types.SimpleNamespace(status=self.statuses[i])


def run(coro):
    return asyncio.run(coro)


def setup(monkeypatch, sightings=2):
    clock = FakeClock()
    monkeypatch.setattr(scheduling, "time", types.SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(scheduling, "BAN_CONFIRM_GAP_S", 0)
    monkeypatch.setattr(scheduling, "BAN_CONFIRM_SIGHTINGS", sightings)
    return clock


def res(st):
    return types.SimpleNamespace(status=st)


def test_second_unavailable_confirms(monkeypatch):
    setup(monkeypatch)
    s = TierScheduler()
    out = run(s.confirm_ban(Checker(["unavailable"]), "u", res("unavailable")))
    assert out.status == "unavailable"
    assert "u" not in s.ban_seen


def test_live_account_glitch_is_not_a_ban(monkeypatch):
    setup(monkeypatch)
    s = TierScheduler()
    out = run(s.confirm_ban(Checker(["active"]), "u", res("unavailable")))
    assert out.status == "active"
    assert "u" not in s.ban_seen


def test_blocked_answers_keep_first_sighting_and_never_confirm_alone(monkeypatch):
    setup(monkeypatch)
    s = TierScheduler()
    c = Checker(["rate_limited"])
    out = run(s.confirm_ban(c, "u", res("unavailable")))
    assert out.status == "rate_limited"          # no alert yet
    assert c.calls == scheduling.BAN_CONFIRM_TRIES
    assert "u" in s.ban_seen                     # but the sighting is kept


def test_later_round_second_sighting_confirms(monkeypatch):
    clock = setup(monkeypatch)
    s = TierScheduler()
    run(s.confirm_ban(Checker(["rate_limited"]), "u", res("unavailable")))
    clock.t += 60
    out = run(s.confirm_ban(Checker(["unused"]), "u", res("unavailable")))
    assert out.status == "unavailable"
    assert "u" not in s.ban_seen


def test_sighting_expires(monkeypatch):
    clock = setup(monkeypatch)
    s = TierScheduler()
    run(s.confirm_ban(Checker(["rate_limited"]), "u", res("unavailable")))
    clock.t += scheduling.BAN_CONFIRM_WINDOW_S + 1
    out = run(s.confirm_ban(Checker(["rate_limited"]), "u", res("unavailable")))
    assert out.status == "rate_limited"          # stale sighting does not count


def test_active_in_between_cancels(monkeypatch):
    clock = setup(monkeypatch)
    s = TierScheduler()
    run(s.confirm_ban(Checker(["rate_limited"]), "u", res("unavailable")))
    s.note_status("u", "active")
    clock.t += 30
    out = run(s.confirm_ban(Checker(["rate_limited"]), "u", res("unavailable")))
    assert out.status == "rate_limited"


def test_removed_accounts_are_forgotten(monkeypatch):
    setup(monkeypatch)
    s = TierScheduler()
    s.ban_seen["gone"] = [1.0]
    s.due_groups([{"username": "other", "created_at": None}])
    assert "gone" not in s.ban_seen


def test_three_sightings_default_needs_three(monkeypatch):
    clock = setup(monkeypatch, sightings=3)
    s = TierScheduler()
    # first sighting + loop sees: unavailable, blocked, unavailable -> 3 sightings -> confirmed
    out = run(s.confirm_ban(Checker(["unavailable", "rate_limited", "unavailable"]), "u", res("unavailable")))
    assert out.status == "unavailable"
    assert "u" not in s.ban_seen


def test_three_sightings_two_are_not_enough(monkeypatch):
    clock = setup(monkeypatch, sightings=3)
    s = TierScheduler()
    out = run(s.confirm_ban(Checker(["unavailable", "rate_limited"]), "u", res("unavailable")))
    assert out.status == "rate_limited"          # only 2 sightings so far: no alert
    assert len(s.ban_seen["u"]) == 2
    clock.t += 60                                # next round brings the 3rd
    out = run(s.confirm_ban(Checker(["rate_limited"]), "u", res("unavailable")))
    assert out.status == "unavailable"


def test_three_sightings_active_in_loop_cancels(monkeypatch):
    setup(monkeypatch, sightings=3)
    s = TierScheduler()
    out = run(s.confirm_ban(Checker(["unavailable", "active"]), "u", res("unavailable")))
    assert out.status == "active"
    assert "u" not in s.ban_seen
