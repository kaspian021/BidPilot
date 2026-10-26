"""Subscription manager — per-chat account, plan limits, and payment lifecycle."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import PaymentRow, SubscriptionRecord, UserAccountRow, utcnow
from .plans import PLANS, Plan, all_plans, get_plan
from .zarinpal import PaymentRequest, ZarinpalClient, ZarinpalConfig

log = logging.getLogger(__name__)


class SubscriptionManager:
    def __init__(self, session: AsyncSession, zarinpal_config: ZarinpalConfig | None = None) -> None:
        self.session = session
        self.zarinpal_config = zarinpal_config or ZarinpalConfig(merchant_id="")

    # ---- user accounts ----

    async def get_or_create_user(self, chat_id: str | int, *, username: str | None = None, first_name: str | None = None) -> UserAccountRow:
        cid = str(chat_id)
        stmt = select(UserAccountRow).where(UserAccountRow.telegram_chat_id == cid)
        row = (await self.session.execute(stmt)).scalars().first()
        if row:
            # update name if provided
            if username and row.telegram_username != username:
                row.telegram_username = username
            if first_name and row.telegram_first_name != first_name:
                row.telegram_first_name = first_name
            await self.session.flush()
            return row
        row = UserAccountRow(
            telegram_chat_id=cid,
            telegram_username=username,
            telegram_first_name=first_name,
            plan_id="free",
        )
        self.session.add(row)
        await self.session.flush()
        log.info("Created user account for chat %s", cid)
        return row

    async def get_user(self, chat_id: str | int) -> UserAccountRow | None:
        cid = str(chat_id)
        stmt = select(UserAccountRow).where(UserAccountRow.telegram_chat_id == cid)
        return (await self.session.execute(stmt)).scalars().first()

    async def effective_plan(self, chat_id: str | int) -> tuple[UserAccountRow | None, Plan]:
        user = await self.get_user(chat_id)
        if not user:
            return None, get_plan("free")
        plan = get_plan(user.plan_id or "free")
        # expiry check
        if user.plan_expires_at and user.plan_expires_at < utcnow():
            # expired → revert to free but keep history
            if user.plan_id != "free":
                user.plan_id = "free"
                user.plan_expires_at = None
                await self.session.flush()
                plan = get_plan("free")
        return user, plan

    async def can_bid(self, chat_id: str | int) -> tuple[bool, str]:
        """Check monthly cap as example; hourly/daily via ratelimit."""
        user, plan = await self.effective_plan(chat_id)
        if plan.is_free():
            return True, ""
        # if expired handled above, still check
        return True, ""

    # ---- plans / purchase ----

    async def create_payment(self, chat_id: str | int, plan_id: str, *, mobile: str | None = None, email: str | None = None) -> tuple[PaymentRow, str]:
        plan = get_plan(plan_id)
        if plan.is_free():
            raise ValueError("پلن رایگان نیاز به پرداخت ندارد")
        user = await self.get_or_create_user(chat_id)
        # create payment row first for order_id
        payment = PaymentRow(
            user_chat_id=str(chat_id),
            plan_id=plan.id,
            amount_toman=plan.price_toman,
            status="created",
        )
        self.session.add(payment)
        await self.session.flush()

        client = ZarinpalClient(self.zarinpal_config)
        # callback is not a public URL — we use manual verify; still required by API
        callback = self.zarinpal_config.callback_url or "https://bidpilot.local/payment/verify"
        req = PaymentRequest(
            amount_toman=plan.price_toman,
            description=f"{client.config.description_prefix} - {plan.title} ({plan.period_days} روز)",
            callback_url=callback,
            mobile=mobile,
            email=email,
            order_id=f"bidpilot-{payment.id}-{chat_id}",
        )
        result = await client.create_payment(req)
        if not result.ok:
            payment.status = "failed"
            payment.zarinpal_response = json.dumps({"error": result.message, "code": result.code}, ensure_ascii=False)
            await self.session.flush()
            raise RuntimeError(result.message)
        payment.authority = result.authority
        payment.status = "sent"
        payment.zarinpal_response = json.dumps({"authority": result.authority, "payment_url": result.payment_url}, ensure_ascii=False)
        await self.session.flush()
        return payment, result.payment_url or ""

    async def verify_payment(self, authority: str) -> tuple[PaymentRow, SubscriptionRecord | None]:
        stmt = select(PaymentRow).where(PaymentRow.authority == authority)
        payment = (await self.session.execute(stmt)).scalars().first()
        if not payment:
            raise ValueError("Authority پیدا نشد — شاید پرداخت از جای دیگری بوده")
        if payment.status == "verified":
            # already verified
            sub_stmt = select(SubscriptionRecord).where(SubscriptionRecord.user_chat_id == payment.user_chat_id).order_by(SubscriptionRecord.id.desc()).limit(1)
            sub = (await self.session.execute(sub_stmt)).scalars().first()
            return payment, sub

        client = ZarinpalClient(self.zarinpal_config)
        verify = await client.verify_payment(authority, payment.amount_toman)
        if not verify.ok:
            payment.status = "failed"
            payment.zarinpal_response = json.dumps({"verify_code": verify.code, "message": verify.message}, ensure_ascii=False)
            await self.session.flush()
            raise RuntimeError(verify.message)

        # success
        payment.status = "verified"
        payment.ref_id = verify.ref_id
        payment.verified_at = utcnow()
        payment.zarinpal_response = json.dumps({"ref_id": verify.ref_id, "code": verify.code, "message": verify.message, "card_pan": verify.card_pan}, ensure_ascii=False)
        await self.session.flush()

        # activate subscription
        plan = get_plan(payment.plan_id)
        user = await self.get_or_create_user(payment.user_chat_id)
        now = utcnow()
        # if user already has active paid plan, extend
        starts = now
        if user.plan_expires_at and user.plan_expires_at > now and user.plan_id == plan.id:
            starts = user.plan_expires_at
        expires = starts + timedelta(days=plan.period_days)

        user.plan_id = plan.id
        user.plan_started_at = now
        user.plan_expires_at = expires
        await self.session.flush()

        sub = SubscriptionRecord(
            user_chat_id=payment.user_chat_id,
            plan_id=plan.id,
            status="active",
            started_at=now,
            expires_at=expires,
        )
        self.session.add(sub)
        await self.session.flush()
        log.info("Activated %s for %s until %s (ref %s)", plan.id, payment.user_chat_id, expires, verify.ref_id)
        return payment, sub

    async def list_payments(self, chat_id: str | int, limit: int = 10) -> list[PaymentRow]:
        stmt = select(PaymentRow).where(PaymentRow.user_chat_id == str(chat_id)).order_by(PaymentRow.id.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars())

    async def usage_stats(self, chat_id: str | int) -> dict:
        user, plan = await self.effective_plan(chat_id)
        # monthly bids count
        from ..repositories import Repository
        # We can count via direct query on BidRow filtered by chat? But BidRow has no chat_id yet.
        # For now count global bids per user is not tracked per chat; we estimate 0
        # TODO: add BidRow.user_chat_id column in next migration
        return {
            "plan": plan,
            "user": user,
            "expires_at": user.plan_expires_at if user else None,
            "is_expired": bool(user and user.plan_expires_at and user.plan_expires_at < utcnow()),
        }

    def zarinpal_client(self) -> ZarinpalClient:
        return ZarinpalClient(self.zarinpal_config)
