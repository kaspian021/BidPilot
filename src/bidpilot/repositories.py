"""Repository helpers — the only place that talks SQL."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import BidRow, EventRow, ProjectRow
from .models import BidPlan, Event, EventKind, Project, SubmissionStatus


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class Repository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ---------------------------------------------------------------- projects
    async def project_exists(self, source: str, external_id: str) -> bool:
        stmt = select(ProjectRow.id).where(ProjectRow.source == source, ProjectRow.external_id == external_id)
        return (await self.session.execute(stmt)).first() is not None

    async def save_project(self, project: Project) -> ProjectRow:
        budget = project.budget
        row = ProjectRow(
            source=project.source,
            external_id=project.external_id,
            title=project.title,
            url=project.url,
            description=project.description,
            budget_min=budget.min if budget else None,
            budget_max=budget.max if budget else None,
            budget_text=budget.as_text() if budget else "",
            skills=json.dumps(project.skills, ensure_ascii=False),
            category=project.category,
            employer=project.employer,
            published_at=project.published_at,
            fetched_at=project.fetched_at,
            raw_json=json.dumps(project.raw, ensure_ascii=False, default=str),
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def get_project_row(self, project_id: int) -> ProjectRow | None:
        return await self.session.get(ProjectRow, project_id)

    async def recent_projects(self, limit: int = 20, source: str | None = None) -> list[ProjectRow]:
        stmt = select(ProjectRow).order_by(ProjectRow.id.desc()).limit(limit)
        if source:
            stmt = stmt.where(ProjectRow.source == source)
        return list((await self.session.execute(stmt)).scalars())

    # -------------------------------------------------------------------- bids
    async def save_bid(self, project_row_id: int, plan: BidPlan) -> BidRow:
        result = plan.result
        row = BidRow(
            project_id=project_row_id,
            source=plan.project.source,
            status=result.status.value if result else SubmissionStatus.SKIPPED.value,
            amount=plan.amount,
            delivery_days=plan.delivery_days,
            proposal_text=plan.proposal.text if plan.proposal else "",
            proposal_generated_by=plan.proposal.generated_by if plan.proposal else "",
            pricing_rationale=plan.rationale,
            message=result.message if result else "",
            external_bid_id=result.external_bid_id if result else None,
            http_status=result.http_status if result else None,
            latency_ms=float(plan.proposal.latency_ms)
            if plan.proposal and plan.proposal.latency_ms
            else None,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def update_bid_status(self, bid_id: int, status: SubmissionStatus, message: str = "") -> None:
        row = await self.session.get(BidRow, bid_id)
        if row:
            row.status = status.value
            row.attempts += 1
            if message:
                row.message = message
            await self.session.flush()

    async def bids_since(self, since: datetime, status: str | None = SubmissionStatus.SUBMITTED.value) -> int:
        stmt = select(func.count()).select_from(BidRow).where(BidRow.created_at >= since)
        if status:
            stmt = stmt.where(BidRow.status == status)
        return int((await self.session.execute(stmt)).scalar_one())

    async def last_bid_at(self) -> datetime | None:
        stmt = select(func.max(BidRow.created_at)).where(BidRow.status == SubmissionStatus.SUBMITTED.value)
        return _aware((await self.session.execute(stmt)).scalar_one_or_none())

    async def recent_bids(self, limit: int = 20) -> list[BidRow]:
        stmt = select(BidRow).order_by(BidRow.id.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars())

    async def stats(self) -> dict[str, int]:
        projects = int(
            (await self.session.execute(select(func.count()).select_from(ProjectRow))).scalar_one()
        )
        by_status = await self.session.execute(select(BidRow.status, func.count()).group_by(BidRow.status))
        counts = {status: int(count) for status, count in by_status}
        counts["projects"] = projects
        return counts

    async def bids_per_hour(self, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        return await self.bids_since(now - timedelta(hours=1))

    async def bids_per_day(self, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        return await self.bids_since(now - timedelta(days=1))

    # ------------------------------------------------------------------ events
    async def log_event(self, event: Event) -> None:
        self.session.add(
            EventRow(
                kind=event.kind.value,
                title=event.title[:500],
                body=event.body,
                project_uid=event.project.uid if event.project else None,
            )
        )
        await self.session.flush()

    async def recent_events(self, limit: int = 30) -> list[EventRow]:
        stmt = select(EventRow).order_by(EventRow.id.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars())

    async def last_event_at(self, kind: str, title: str) -> datetime | None:
        stmt = select(func.max(EventRow.created_at)).where(EventRow.kind == kind, EventRow.title == title)
        return _aware((await self.session.execute(stmt)).scalar_one_or_none())

    async def commit(self) -> None:
        await self.session.commit()

    async def rollback(self) -> None:
        await self.session.rollback()


__all__ = ["Repository", "EventKind"]
