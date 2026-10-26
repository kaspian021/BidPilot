"""Referral system — invite friends via personal link to upgrade plan.

Design: professional, beautiful, no spam.

- Each user gets a stable referral_code (6 chars base36 from sha256(chat_id+pepper))
- Deep link: https://t.me/<bot_username>?start=ref_<code>
- When invitee joins via link (/start ref_XXXX), we record referred_by
- Rewards are milestone-based and stack with paid plans:

  • 3 invites who finish setup (profile + market) → 7 days Starter
  • 5 invites → 15 days Starter (or 7 days Pro if already Starter)
  • 10 invites → 30 days Pro
  • 20 invites → 30 days Elite
  • Each invited user who *purchases* a paid plan → +7 days of same plan to inviter
    (uncapped, instant)

- Progress UI: 🎯 milestone bar, invite count, next reward preview
- Anti-abuse: one invitee counted once, self-referral blocked, duplicate ignored
"""

from __future__ import annotations

import hashlib
import logging
from datetime import timedelta, timezone, datetime

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from .models import UserAccountRow, ReferralRow, utcnow
from .plans import get_plan

log = logging.getLogger(__name__)


# Milestones sorted ascending
MILESTONES: list[dict] = [
    {"count": 3, "reward_plan": "starter", "days": 7, "title": "۷ روز استارتر", "emoji": "🥉"},
    {"count": 5, "reward_plan": "starter", "days": 15, "title": "۱۵ روز استارتر", "emoji": "🥈"},
    {"count": 10, "reward_plan": "pro", "days": 30, "title": "۳۰ روز حرفه‌ای", "emoji": "🥇"},
    {"count": 20, "reward_plan": "elite", "days": 30, "title": "۳۰ روز سازمانی", "emoji": "👑"},
]

# Per-purchase bonus
PURCHASE_BONUS_DAYS = 7


def generate_referral_code(chat_id: str | int, pepper: str = "BidPilot-v1") -> str:
    """Stable 6-char code, collision-resistant for chat_id space."""
    raw = f"{chat_id}:{pepper}".encode()
    h = hashlib.sha256(raw).hexdigest()
    # base36 of first 9 hex chars → ~6 chars
    num = int(h[:9], 16)
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    out = ""
    for _ in range(6):
        out = chars[num % 36] + out
        num //= 36
    return out


class ReferralManager:
    def __init__(self, session: AsyncSession, *, bot_username: str = ""):
        self.session = session
        self.bot_username = (bot_username or "").lstrip("@")

    async def ensure_code(self, chat_id: str | int) -> str:
        """Ensure user has referral_code, return it."""
        user = await self._get_user(str(chat_id))
        if user and getattr(user, "referral_code", None):
            return str(user.referral_code)
        code = generate_referral_code(str(chat_id))
        # Handle rare collision by appending 1 char
        for attempt in range(3):
            stmt = select(UserAccountRow).where(UserAccountRow.referral_code == code, UserAccountRow.telegram_chat_id != str(chat_id))
            exists = (await self.session.execute(stmt)).scalars().first()
            if not exists:
                break
            code = generate_referral_code(f"{chat_id}:{attempt}")
        if user:
            user.referral_code = code
            await self.session.flush()
        return code

    async def invite_link(self, chat_id: str | int) -> tuple[str, str]:
        code = await self.ensure_code(chat_id)
        base = f"https://t.me/{self.bot_username}" if self.bot_username else "https://t.me/BidPilotBot"
        link = f"{base}?start=ref_{code}"
        return code, link

    async def record_referral(self, invitee_chat_id: str | int, referral_code: str) -> bool:
        """Called on /start ref_XXXX. Returns True if counted."""
        referral_code = (referral_code or "").strip().upper()
        if not referral_code:
            return False
        invitee = str(invitee_chat_id)
        # find inviter
        stmt = select(UserAccountRow).where(UserAccountRow.referral_code == referral_code)
        inviter = (await self.session.execute(stmt)).scalars().first()
        if not inviter:
            return False
        if str(inviter.telegram_chat_id) == invitee:
            return False  # self
        # already referred?
        invitee_user = await self._get_user(invitee)
        if invitee_user and getattr(invitee_user, "referred_by", None):
            return False
        # duplicate referral row?
        dup = (await self.session.execute(select(ReferralRow).where(ReferralRow.invitee_chat_id == invitee))).scalars().first()
        if dup:
            return False
        # create user for invitee if missing (so referred_by persists)
        if not invitee_user:
            invitee_user = UserAccountRow(telegram_chat_id=invitee, plan_id="free", referral_code=generate_referral_code(invitee))
            self.session.add(invitee_user)
            await self.session.flush()
        invitee_user.referred_by = str(inviter.telegram_chat_id)
        # create referral row
        row = ReferralRow(
            inviter_chat_id=str(inviter.telegram_chat_id),
            invitee_chat_id=invitee,
            invitee_username=getattr(invitee_user, "telegram_username", None),
            status="pending",
        )
        self.session.add(row)
        await self.session.flush()
        log.info("Referral recorded: %s -> %s via %s", inviter.telegram_chat_id, invitee, referral_code)
        return True

    async def mark_setup_complete(self, chat_id: str | int) -> list[dict]:
        """When invitee finishes setup (profile+market), mark referral qualified and grant rewards."""
        invitee = str(chat_id)
        stmt = select(ReferralRow).where(ReferralRow.invitee_chat_id == invitee, ReferralRow.status == "pending")
        row = (await self.session.execute(stmt)).scalars().first()
        if not row:
            return []
        row.status = "qualified"
        row.qualified_at = utcnow()
        await self.session.flush()
        # check milestones for inviter
        return await self.check_and_grant_milestones(row.inviter_chat_id)

    async def mark_purchase(self, chat_id: str | int, plan_id: str) -> list[dict]:
        """When invitee purchases paid plan, grant purchase bonus to inviter."""
        invitee = str(chat_id)
        # find referral for this invitee
        stmt = select(ReferralRow).where(ReferralRow.invitee_chat_id == invitee)
        row = (await self.session.execute(stmt)).scalars().first()
        if not row:
            return []
        # grant bonus days to inviter
        inviter_id = row.inviter_chat_id
        granted = await self._grant_days(inviter_id, plan_id, PURCHASE_BONUS_DAYS, f"دعوت موفق: خرید {plan_id} توسط دوست")
        # also count as milestone progress (purchases also qualify)
        if row.status == "pending":
            row.status = "qualified"
            row.qualified_at = utcnow()
            await self.session.flush()
            ms = await self.check_and_grant_milestones(inviter_id)
            granted.extend(ms)
        return granted

    async def check_and_grant_milestones(self, inviter_chat_id: str) -> list[dict]:
        """Count qualified invites and grant unclaimed milestones."""
        cid = str(inviter_chat_id)
        cnt_stmt = select(func.count()).select_from(ReferralRow).where(ReferralRow.inviter_chat_id == cid, ReferralRow.status.in_(["qualified", "purchased"]))
        total = int((await self.session.execute(cnt_stmt)).scalar_one())
        # find milestones not yet granted
        # We'll track granted milestones via a simple tableless: check if user already had that many days?
        # Simpler: store granted_milestone_count in UserAccountRow as JSON list
        user = await self._get_user(cid)
        if not user:
            return []
        granted_list: list[int] = []
        try:
            import json
            raw = getattr(user, "granted_milestones", None) or "[]"
            granted_list = json.loads(raw) if isinstance(raw, str) else list(raw)
        except Exception:
            granted_list = []
        new_grants = []
        for m in MILESTONES:
            if total >= m["count"] and m["count"] not in granted_list:
                # grant
                await self._grant_days(cid, m["reward_plan"], m["days"], f"مایل‌استون {m['count']} دعوت")
                granted_list.append(m["count"])
                new_grants.append(m)
        if new_grants:
            import json
            user.granted_milestones = json.dumps(granted_list, ensure_ascii=False)
            await self.session.flush()
        return new_grants

    async def _grant_days(self, chat_id: str, plan_id: str, days: int, reason: str) -> list[dict]:
        """Extend plan by days; if user is free or lower plan, upgrade."""
        user = await self._get_user(str(chat_id))
        if not user:
            return []
        from datetime import timezone
        now = utcnow()
        # decide target plan: if user's current plan is lower rank than reward, upgrade
        rank = {"free": 0, "starter": 1, "pro": 2, "elite": 3}
        cur_rank = rank.get(str(user.plan_id or "free"), 0)
        rew_rank = rank.get(plan_id, 1)
        target_plan = plan_id if rew_rank > cur_rank else str(user.plan_id or "free")
        # extend
        base = user.plan_expires_at if user.plan_expires_at and user.plan_expires_at > now else now
        # if switching to higher plan, start from now
        if target_plan != user.plan_id:
            base = now
            user.plan_id = target_plan
            user.plan_started_at = now
        user.plan_expires_at = base + timedelta(days=days)
        await self.session.flush()
        log.info("Referral grant %s days %s to %s reason=%s", days, target_plan, chat_id, reason)
        return [{"plan": target_plan, "days": days, "reason": reason}]

    async def stats(self, chat_id: str | int) -> dict:
        cid = str(chat_id)
        total_q = select(func.count()).select_from(ReferralRow).where(ReferralRow.inviter_chat_id == cid, ReferralRow.status.in_(["qualified", "purchased"]))
        total_pend = select(func.count()).select_from(ReferralRow).where(ReferralRow.inviter_chat_id == cid, ReferralRow.status == "pending")
        total = int((await self.session.execute(total_q)).scalar_one())
        pending = int((await self.session.execute(total_pend)).scalar_one())
        # next milestone
        nxt = next((m for m in MILESTONES if total < m["count"]), None)
        # recent invites
        recent = (await self.session.execute(select(ReferralRow).where(ReferralRow.inviter_chat_id == cid).order_by(ReferralRow.created_at.desc()).limit(5))).scalars().all()
        code, link = await self.invite_link(cid)
        return {
            "total_qualified": total,
            "pending": pending,
            "next_milestone": nxt,
            "recent": recent,
            "code": code,
            "link": link,
        }

    async def _get_user(self, chat_id: str):
        stmt = select(UserAccountRow).where(UserAccountRow.telegram_chat_id == str(chat_id))
        return (await self.session.execute(stmt)).scalars().first()
