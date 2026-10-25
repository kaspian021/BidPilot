"""Mock marketplace — powers ``bidpilot demo`` and the test-suite.

It reads fixture projects from JSON and simulates submissions, including the failure
modes the real world throws at you (already-bid, rate limited, expired session).
"""

from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path
from typing import Any

import httpx

from ...models import Project, Proposal, SubmissionResult, SubmissionStatus
from ..base import Source
from ..registry import register_source

_FIXTURE = Path(__file__).with_name("fixtures") / "projects.json"


@register_source("mock")
class MockSource(Source):
    name = "mock"
    # Simulate the marketplace's current proposal state for deterministic tests.
    requires_bid_status_check = True

    def __init__(self, settings, config, secrets, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(settings, config, secrets, client)
        self.submitted: list[dict[str, Any]] = []
        self._submitted_uids: set[str] = set()
        self.failure_rate = float(self.settings.get("failure_rate", 0.0))

    async def fetch_projects(self) -> list[Project]:
        raw = self.settings.get("projects")
        if raw is None:
            fixture = Path(self.settings.get("fixture_path", _FIXTURE))
            raw = json.loads(fixture.read_text(encoding="utf-8"))
        await asyncio.sleep(float(self.settings.get("latency", 0)))
        return [Project(source=self.name, **item) for item in raw]

    async def check_bid_status(self, project: Project) -> bool:
        """Return the mock marketplace's state, independent of database history."""
        return project.uid in self._submitted_uids

    async def submit_bid(
        self,
        project: Project,
        proposal: Proposal,
        amount: int,
        delivery_days: int | None,
    ) -> SubmissionResult:
        await asyncio.sleep(float(self.settings.get("submit_latency", 0)))
        if self.failure_rate and random.random() < self.failure_rate:
            return self.fail("شبیه‌سازی خطای سایت (mock failure_rate).", 500)
        self.submitted.append(
            {
                "uid": project.uid,
                "amount": amount,
                "delivery_days": delivery_days,
                "proposal": proposal.text,
            }
        )
        self._submitted_uids.add(project.uid)
        return SubmissionResult(
            status=SubmissionStatus.SUBMITTED,
            message="پیشنهاد در محیط آزمایشی ثبت شد.",
            external_bid_id=f"mock-{len(self.submitted)}",
            http_status=200,
        )

    async def healthcheck(self) -> tuple[bool, str]:
        return True, "mock source ready"
