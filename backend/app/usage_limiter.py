"""Anonymous usage limiting: 10 successful validations per session per day.

Design (no Redis required):

- One SQLite row per (session_id, IST calendar day).
- ``successful_validation_count`` - completed analyses.
- ``reserved_count``            - capacity held by in-flight validations.

The API reserves one unit *before* doing expensive work (atomic single-statement
``INSERT ... ON CONFLICT ... WHERE ... RETURNING``), commits it after a
successful analysis, or releases it on any failure. Concurrent requests
therefore cannot exceed the limit, and failed runs never consume allowance.

The IST calendar day is computed on the server; client-supplied dates are
never trusted. Test code can freeze the clock by injecting ``clock``.
"""

from __future__ import annotations

import logging
import re
import uuid
import zoneinfo
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .config import settings
from .schemas import UsageSnapshot

logger = logging.getLogger(__name__)

_SESSION_RE = None


def validate_session_id(session_id: str | None) -> str | None:
    """Return the id when it passes the anonymous-session format, else None."""
    global _SESSION_RE
    if not session_id or not isinstance(session_id, str):
        return None
    if _SESSION_RE is None:
        _SESSION_RE = re.compile(settings.session_id_pattern)
    if _SESSION_RE.fullmatch(session_id):
        return session_id
    return None


def generate_session_id() -> str:
    """Format a fresh anonymous session id (used by the frontend contract)."""
    return uuid.uuid4().hex


class UsageLimiter:
    """Persistence-backed daily allowance tracker."""

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Callable[[], datetime] | None = None,
        daily_limit: int | None = None,
        reservation_ttl_seconds: int | None = None,
    ):
        self.engine = engine
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.daily_limit = daily_limit if daily_limit is not None else settings.daily_limit
        self.ttl = (
            reservation_ttl_seconds
            if reservation_ttl_seconds is not None
            else settings.reservation_ttl_seconds
        )

    # ------------------------------------------------------------------ time
    @property
    def _tz(self) -> zoneinfo.ZoneInfo:
        return zoneinfo.ZoneInfo(settings.timezone_name)

    def _now(self) -> datetime:
        return self.clock().astimezone(timezone.utc).replace(tzinfo=None)

    def ist_now(self) -> datetime:
        return self.clock().astimezone(self._tz)

    def ist_today(self) -> date:
        return self.ist_now().date()

    def reset_at(self) -> datetime:
        """Next midnight in the configured (IST) calendar."""
        ist_now = self.ist_now()
        next_midnight = (ist_now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return next_midnight

    # ------------------------------------------------------------- internals
    def snapshot(self, session_id: str) -> UsageSnapshot:
        today = self.ist_today()
        used, reserved = self._counts(session_id, today)
        remaining = max(0, self.daily_limit - used - reserved)
        return UsageSnapshot(
            daily_limit=self.daily_limit,
            successful_validations=used,
            remaining_validations=remaining,
            usage_date=today,
            reset_at=self.reset_at(),
            timezone=settings.timezone_name,
        )

    def _counts(self, session_id: str, today: date | None) -> tuple[int, int]:
        """(successful, live reserved) counts; ``today`` defaults to current day.

        Expired reservations are cleared for the session before reading, for
        the day that owns them (they may belong to a previous IST day).
        """
        stale_before = self._now() - timedelta(seconds=self.ttl)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE usage_daily SET reserved_count = 0 "
                    "WHERE session_id = :sid AND last_reserve_at IS NOT NULL "
                    "AND last_reserve_at < :stale_before"
                ),
                {"sid": session_id, "stale_before": stale_before},
            )
            row = conn.execute(
                text(
                    "SELECT successful_validation_count, reserved_count FROM usage_daily "
                    "WHERE session_id = :sid AND usage_date = :today"
                ),
                {"sid": session_id, "today": today},
            ).first()
        if row is None:
            return 0, 0
        return int(row[0]), int(row[1])

    # ---------------------------------------------------------------- public
    def try_reserve(self, session_id: str) -> date | None:
        """Atomically claim one unit of today's allowance.

        Returns the reserved IST day (needed to commit/release) or None when
        the allowance for the day is exhausted.
        """
        today = self.ist_today()
        now = self._now()
        reserved_day: date | None = None
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE usage_daily SET reserved_count = 0, last_reserve_at = NULL "
                    "WHERE session_id = :sid "
                    "AND last_reserve_at IS NOT NULL AND last_reserve_at < :stale_before"
                ),
                {"sid": session_id, "stale_before": now - timedelta(seconds=self.ttl)},
            )
            row = conn.execute(
                text(
                    "INSERT INTO usage_daily (session_id, usage_date, "
                    "successful_validation_count, reserved_count, last_reserve_at) "
                    "VALUES (:sid, :today, 0, 1, :now) "
                    "ON CONFLICT(session_id, usage_date) DO UPDATE SET "
                    "  reserved_count = usage_daily.reserved_count + 1, "
                    "  last_reserve_at = :now2 "
                    "WHERE usage_daily.successful_validation_count "
                    "      + usage_daily.reserved_count < :limit "
                    "RETURNING usage_date, reserved_count"
                ),
                {
                    "sid": session_id,
                    "today": today,
                    "now": now,
                    "now2": now,
                    "limit": self.daily_limit,
                },
            ).first()
            if row is None:
                return None
            reserved_day = row[0]
            if isinstance(reserved_day, str):
                # SQLite RETURNING yields the raw ISO text for date columns
                reserved_day = date.fromisoformat(reserved_day)
        return reserved_day

    def commit(self, session_id: str, usage_date: date) -> None:
        """Convert a reservation into a counted successful validation."""
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE usage_daily SET "
                    "  reserved_count = CASE WHEN reserved_count > 0 THEN reserved_count - 1 ELSE 0 END, "
                    "  successful_validation_count = successful_validation_count + 1, "
                    "  last_reserve_at = NULL "
                    "WHERE session_id = :sid AND usage_date = :today"
                ),
                {"sid": session_id, "today": usage_date},
            )

    def release(self, session_id: str, usage_date: date) -> None:
        """Return a reservation to the pool after a failed validation."""
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE usage_daily SET "
                    "  reserved_count = CASE WHEN reserved_count > 0 THEN reserved_count - 1 ELSE 0 END, "
                    "  last_reserve_at = NULL "
                    "WHERE session_id = :sid AND usage_date = :today"
                ),
                {"sid": session_id, "today": usage_date},
            )


def new_usage_limiter(engine: Engine, **kwargs) -> UsageLimiter:
    return UsageLimiter(engine, **kwargs)
