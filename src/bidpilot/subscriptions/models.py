"""Subscription and account records — reuses the global DB but adds per-chat tables."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from bidpilot.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes on every backend.

    SQLite stores ``UTCDateTime()`` without the offset and hands back
    *naive* values; comparing those with ``utcnow()`` raised ``TypeError`` and the
    plan of every paid/referral user silently fell back to "free".
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


class UserAccountRow(Base):
    __tablename__ = "user_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_chat_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    telegram_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    telegram_first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    plan_id: Mapped[str] = mapped_column(String(32), default="free")
    plan_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    plan_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=func.now(), onupdate=func.now())
    # JSON blob for per-user settings mirroring settings_store (profile, filters, etc.)
    settings_json: Mapped[str] = mapped_column(Text, default="{}")
    # stats cache
    total_bids: Mapped[int] = mapped_column(Integer, default=0)
    successful_bids: Mapped[int] = mapped_column(Integer, default=0)
    # Referral
    referral_code: Mapped[str | None] = mapped_column(String(16), nullable=True, unique=True, index=True)
    referred_by: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    granted_milestones: Mapped[str] = mapped_column(Text, default="[]")  # JSON list of counts


class SubscriptionRecord(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_chat_id: Mapped[str] = mapped_column(String(32), index=True)
    plan_id: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending, active, expired, cancelled
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    auto_renew: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=func.now())


class PaymentRow(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_chat_id: Mapped[str] = mapped_column(String(32), index=True)
    plan_id: Mapped[str] = mapped_column(String(32))
    amount_toman: Mapped[int] = mapped_column(Integer)
    authority: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    ref_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="created")  # created, sent, verified, failed, cancelled
    gateway: Mapped[str] = mapped_column(String(32), default="zarinpal")
    zarinpal_response: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=func.now())
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class ReferralRow(Base):
    __tablename__ = "referrals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    inviter_chat_id: Mapped[str] = mapped_column(String(32), index=True)
    invitee_chat_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    invitee_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending, qualified, purchased
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=func.now())
    qualified_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
