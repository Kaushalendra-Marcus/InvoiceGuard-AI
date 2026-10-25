"""IP-level abuse protection for the analyze endpoint.

Separate from the anonymous-session daily allowance (``usage_limiter``):
session ids are client-supplied and can be rotated freely, so this limiter
keys on the client IP (``X-Forwarded-For`` when the app sits behind a proxy,
otherwise the socket address) and enforces two hard caps:

- at most ``rate_limit_per_minute`` analyses per rolling minute bucket,
- at most ``rate_limit_lifetime`` analyses in the lifetime of the database.

Only requests *admitted* by this limiter consume allowance: the atomic
upsert below increments the counters exclusively when both caps still have
room, so a denied burst does not silently drain the lifetime budget.

Design (no Redis required): one SQLite row per IP, updated with a single
``INSERT ... ON CONFLICT ... WHERE ... RETURNING`` statement inside one
transaction — concurrent requests therefore serialize correctly.

Persistence note: the counters live in the same SQLite database as the usage
table. On ephemeral hosting (Render free tier) the disk resets when the
instance restarts, so "lifetime" means *per database lifetime*, and this is
stated in the 429 response.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateDecision:
    """Outcome of one analyze attempt for a client IP."""

    allowed: bool
    scope: str | None  # "minute" | "lifetime" when denied, None when allowed
    minute_count: int  # admitted requests in the current minute bucket
    lifetime_count: int  # admitted requests since the database was created
    seconds_until_minute_reset: int
    per_minute: int
    lifetime: int


class RateLimiter:
    """Persistence-backed per-IP rate limiting (minute + lifetime caps)."""

    def __init__(
        self,
        engine: Engine,
        *,
        per_minute: int | None = None,
        lifetime: int | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.engine = engine
        self.per_minute = (
            per_minute if per_minute is not None else settings.rate_limit_per_minute
        )
        self.lifetime = (
            lifetime if lifetime is not None else settings.rate_limit_lifetime
        )
        self.clock = clock or (lambda: datetime.now().astimezone())
        self._ensure_table()

    # ---------------------------------------------------------------- setup
    def _ensure_table(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS ip_rate_limits ("
                    " ip TEXT PRIMARY KEY, "
                    " minute_bucket TEXT NOT NULL, "
                    " minute_count INTEGER NOT NULL, "
                    " lifetime_count INTEGER NOT NULL)"
                )
            )

    # ------------------------------------------------------------------ time
    def _bucket(self, now: datetime) -> tuple[str, int]:
        """UTC minute-bucket key and seconds remaining until it rolls over."""
        epoch = now.timestamp()
        into_minute = epoch % 60
        bucket_start = epoch - into_minute
        bucket = datetime.fromtimestamp(bucket_start, tz=now.tzinfo).strftime(
            "%Y%m%d%H%M"
        )
        return bucket, max(1, int(60 - into_minute))

    # ---------------------------------------------------------------- public
    def hit(self, ip: str) -> RateDecision:
        """Atomically claim one analyze slot for ``ip``.

        Returns ``allowed=True`` when the request is admitted; both counters
        are incremented only in that case.
        """
        now = self.clock()
        bucket, seconds_left = self._bucket(now)
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    "INSERT INTO ip_rate_limits (ip, minute_bucket, minute_count, "
                    "lifetime_count) "
                    "VALUES (:ip, :bucket, 1, 1) "
                    "ON CONFLICT(ip) DO UPDATE SET "
                    "  minute_count = CASE "
                    "    WHEN ip_rate_limits.minute_bucket = excluded.minute_bucket "
                    "    THEN ip_rate_limits.minute_count + 1 ELSE 1 END, "
                    "  minute_bucket = excluded.minute_bucket, "
                    "  lifetime_count = ip_rate_limits.lifetime_count + 1 "
                    "WHERE (CASE "
                    "    WHEN ip_rate_limits.minute_bucket = excluded.minute_bucket "
                    "    THEN ip_rate_limits.minute_count ELSE 0 END) < :minute_limit "
                    "  AND ip_rate_limits.lifetime_count < :lifetime_limit "
                    "RETURNING minute_count, lifetime_count"
                ),
                {
                    "ip": ip,
                    "bucket": bucket,
                    "minute_limit": self.per_minute,
                    "lifetime_limit": self.lifetime,
                },
            ).first()
            if row is not None:
                return RateDecision(
                    allowed=True,
                    scope=None,
                    minute_count=int(row[0]),
                    lifetime_count=int(row[1]),
                    seconds_until_minute_reset=seconds_left,
                    per_minute=self.per_minute,
                    lifetime=self.lifetime,
                )
            current = conn.execute(
                text(
                    "SELECT minute_bucket, minute_count, lifetime_count "
                    "FROM ip_rate_limits WHERE ip = :ip"
                ),
                {"ip": ip},
            ).first()
        if current is None:
            # Should not happen (a fresh IP always inserts successfully);
            # fail closed with a conservative minute-style denial.
            logger.warning("Rate limiter row missing for ip=%r after denial.", ip)
            return RateDecision(
                allowed=False,
                scope="minute",
                minute_count=0,
                lifetime_count=0,
                seconds_until_minute_reset=seconds_left,
                per_minute=self.per_minute,
                lifetime=self.lifetime,
            )
        minute_count = int(current[1])
        lifetime_count = int(current[2])
        scope = "lifetime" if lifetime_count >= self.lifetime else "minute"
        return RateDecision(
            allowed=False,
            scope=scope,
            minute_count=minute_count,
            lifetime_count=lifetime_count,
            seconds_until_minute_reset=seconds_left,
            per_minute=self.per_minute,
            lifetime=self.lifetime,
        )


def ip_rate_limits_snapshot(engine: Engine, ip: str) -> tuple[int, int]:
    """(minute_count, lifetime_count) as currently stored for ``ip``."""
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT minute_count, lifetime_count FROM ip_rate_limits "
                "WHERE ip = :ip"
            ),
            {"ip": ip},
        ).first()
    if row is None:
        return 0, 0
    return int(row[0]), int(row[1])
