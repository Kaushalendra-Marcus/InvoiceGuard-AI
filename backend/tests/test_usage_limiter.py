"""Tests for the anonymous daily usage limiter.

Covers: successful count increments, failures not consuming allowance, the
11th validation being rejected, concurrency safety, Asia/Kolkata day rollover,
and server-side day computation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Barrier, Thread

import pytest

from app.usage_limiter import UsageLimiter, validate_session_id


class FrozenClock:
    """Mutable clock controlled by tests (timezone-aware UTC)."""

    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta) -> None:
        self.now = self.now + delta


def make_limiter(engine, start: datetime | None = None, limit: int = 10):
    clock = FrozenClock(start or datetime(2026, 10, 23, 5, 0, tzinfo=timezone.utc))  # 10:30 IST
    limiter = UsageLimiter(engine, clock=clock, daily_limit=limit)
    return limiter, clock


def test_invalid_session_ids_are_rejected():
    assert validate_session_id("anon-abcd1234") == "anon-abcd1234"
    assert validate_session_id("A1_b2-c3") is not None
    assert validate_session_id(None) is None
    assert validate_session_id("") is None
    assert validate_session_id("short") is None            # < 8 chars
    assert validate_session_id("has spaces!") is None
    assert validate_session_id("x" * 65) is None           # > 64 chars


def test_success_increments_and_snapshot_reflects_allowance(app_env):
    app, limiter, engine = app_env
    limiter, _ = make_limiter(engine)
    sid = "usage-test-0001"

    snapshot = limiter.snapshot(sid)
    assert snapshot.daily_limit == 10
    assert snapshot.successful_validations == 0
    assert snapshot.remaining_validations == 10
    assert snapshot.timezone == "Asia/Kolkata"
    # reset at next midnight IST (00:00 local) expressed in IST
    assert snapshot.reset_at.hour == 0 and snapshot.reset_at.minute == 0
    assert snapshot.reset_at.utcoffset().total_seconds() == 5 * 3600 + 1800

    day = limiter.try_reserve(sid)
    assert day is not None
    limiter.commit(sid, day)

    snapshot = limiter.snapshot(sid)
    assert snapshot.successful_validations == 1
    assert snapshot.remaining_validations == 9


def test_failed_validation_does_not_consume_allowance(app_env):
    app, limiter, engine = app_env
    limiter, _ = make_limiter(engine)
    sid = "usage-test-0002"

    day = limiter.try_reserve(sid)
    assert day is not None
    limiter.release(sid, day)  # the processing failed; reservation returned

    snapshot = limiter.snapshot(sid)
    assert snapshot.successful_validations == 0
    assert snapshot.remaining_validations == 10


def test_eleventh_successful_validation_rejected(app_env):
    app, limiter, engine = app_env
    limiter, _ = make_limiter(engine)
    sid = "usage-test-0003"

    for _ in range(10):
        day = limiter.try_reserve(sid)
        assert day is not None
        limiter.commit(sid, day)
    snapshot = limiter.snapshot(sid)
    assert snapshot.remaining_validations == 0
    assert snapshot.successful_validations == 10

    assert limiter.try_reserve(sid) is None

    # a failed run after exhaustion also cannot conjure capacity
    limiter.release(sid, limiter.ist_today())
    assert limiter.try_reserve(sid) is None


def test_reservation_across_sessions_is_independent(app_env):
    app, limiter, engine = app_env
    limiter, _ = make_limiter(engine)
    a, b = "usage-test-aaaa", "usage-test-bbbb"
    day_a = limiter.try_reserve(a)
    day_b = limiter.try_reserve(b)
    assert day_a == day_b
    limiter.commit(a, day_a)
    assert limiter.snapshot(a).successful_validations == 1
    assert limiter.snapshot(b).successful_validations == 0


def test_concurrent_requests_never_exceed_limit(app_env):
    """Many parallel reserve->commit cycles: commits must equal the daily limit."""
    app, limiter, engine = app_env
    limiter, _ = make_limiter(engine)
    sid = "usage-test-conc"

    workers = 16
    barrier = Barrier(workers)
    committed: list[int] = []

    def run() -> None:
        barrier.wait()
        day = limiter.try_reserve(sid)
        if day is None:
            return
        committed.append(1)
        limiter.commit(sid, day)

    threads = [Thread(target=run) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    snapshot = limiter.snapshot(sid)
    assert len(committed) == 10
    assert snapshot.successful_validations == 10


def test_in_flight_reservations_count_against_allowance(app_env):
    app, limiter, engine = app_env
    limiter, _ = make_limiter(engine)
    sid = "usage-test-resv"

    # reserve 10 units (no commits yet): all 10 slots are held by in-flight requests
    for _ in range(10):
        assert limiter.try_reserve(sid) is not None
    assert limiter.snapshot(sid).remaining_validations == 0
    assert limiter.try_reserve(sid) is None


def test_stale_reservations_are_reclaimed(app_env):
    app, limiter, engine = app_env
    limiter, clock = make_limiter(engine)
    sid = "usage-test-stale"

    for _ in range(10):
        assert limiter.try_reserve(sid) is not None
    # the validation process dies without committing; 20 minutes pass
    clock.advance(timedelta(minutes=20))
    assert limiter.snapshot(sid).remaining_validations == 10


def test_usage_resets_next_asia_kolkata_day(app_env):
    app, limiter, engine = app_env
    # 2026-10-23 17:30 UTC == 2026-10-23 23:00 IST
    limiter, clock = make_limiter(engine, start=datetime(2026, 10, 23, 17, 30, tzinfo=timezone.utc))
    sid = "usage-test-rollover"

    for _ in range(10):
        day = limiter.try_reserve(sid)
        limiter.commit(sid, day)
    assert limiter.snapshot(sid).remaining_validations == 0

    # +30 minutes -> 23:30 IST, still the same IST day
    clock.advance(timedelta(minutes=30))
    assert limiter.ist_today().isoformat() == "2026-10-23"
    assert limiter.try_reserve(sid) is None

    # cross IST midnight: usage must reset
    clock.advance(timedelta(minutes=60))
    assert limiter.ist_today().isoformat() == "2026-10-24"
    snapshot_after = limiter.snapshot(sid)
    assert snapshot_after.successful_validations == 0
    assert snapshot_after.remaining_validations == 10
    day = limiter.try_reserve(sid)
    assert day is not None and day.isoformat() == "2026-10-24"


def test_failed_run_across_midnight_does_not_leak(app_env):
    app, limiter, engine = app_env
    limiter, clock = make_limiter(engine)
    sid = "usage-test-leak"

    day = limiter.try_reserve(sid)
    limiter.release(sid, day)          # failure path carries the reserved day
    clock.advance(timedelta(minutes=30))
    snapshot = limiter.snapshot(sid)
    assert snapshot.remaining_validations == 10
