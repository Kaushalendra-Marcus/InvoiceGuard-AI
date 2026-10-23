"""SQLAlchemy ORM models."""

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class UsageDaily(Base):
    """Successful-validation counters per anonymous session and IST calendar day.

    ``successful_validation_count`` counts completed analysis runs.
    ``reserved_count`` holds capacity reserved by in-flight validations; it is
    decremented when a validation commits or releases. This makes the daily
    limit concurrency-safe: two parallel requests each reserve one unit of
    capacity before doing expensive work.
    """

    __tablename__ = "usage_daily"
    __table_args__ = (UniqueConstraint("session_id", "usage_date", name="uq_usage_session_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    usage_date: Mapped[date] = mapped_column(Date)
    successful_validation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Timestamp of the most recent reservation; used to expire stale ones.
    last_reserve_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    __mapper_args__ = {"confirm_deleted_rows": False}
