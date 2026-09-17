"""Persistent state: batch budgets and the idempotent authorization ledger."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Batch(Base):
    """A sample batch with a fixed, non-renewable pulse budget."""

    __tablename__ = "batches"
    __table_args__ = (
        CheckConstraint("budget > 0", name="ck_batches_budget_positive"),
        CheckConstraint("remaining >= 0", name="ck_batches_remaining_non_negative"),
    )

    batch_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    budget: Mapped[int] = mapped_column(Integer, nullable=False)
    remaining: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class Authorization(Base):
    """Ledger of successful authorization requests, one row per request_key.

    This table is the idempotency ledger: the unique request_key guarantees
    that a retried request can never deduct twice, and remaining_after stores
    the exact post-deduction balance so retries replay the original response.
    Failed requests (e.g. insufficient budget) are never recorded here.
    """

    __tablename__ = "authorizations"
    __table_args__ = (
        CheckConstraint("pulses > 0", name="ck_authorizations_pulses_positive"),
        CheckConstraint("remaining_after >= 0", name="ck_authorizations_remaining_non_negative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_key: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("batches.batch_id"), nullable=False)
    pulses: Mapped[int] = mapped_column(Integer, nullable=False)
    remaining_after: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
