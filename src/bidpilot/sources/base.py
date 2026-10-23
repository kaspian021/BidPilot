"""Source adapter contract.

Adding a marketplace = subclass ``Source``, implement two methods, register it.
The pipeline, pricing, proposal generation, notifications and persistence are shared.
"""

from __future__ import annotations

import abc
import time
from typing import Any

import httpx

from ..config import AppConfig, Secrets
from ..models import Project, Proposal, SubmissionResult, SubmissionStatus


class SourceError(RuntimeError):
    pass


class Source(abc.ABC):
    name: str = "base"

    def __init__(
        self,
        settings: dict[str, Any],
        config: AppConfig,
        secrets: Secrets,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings or {}
        self.config = config
        self.secrets = secrets
        self._client = client
        self._owned_client = client is None

    # ------------------------------------------------------------------ http
    @property
    def base_url(self) -> str:
        return str(self.settings.get("base_url", "")).rstrip("/")

    def headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": self.config.runtime.user_agent,
            "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.5",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        headers.update(self.settings.get("headers", {}) or {})
        return headers

    def cookies(self) -> dict[str, str]:
        return dict(self.settings.get("cookies", {}) or {})

    async def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=self.headers(),
                cookies=self.cookies(),
                timeout=self.config.runtime.request_timeout_seconds,
                follow_redirects=True,
                http2=bool(self.settings.get("http2", False)),
            )
            self._owned_client = True
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owned_client:
            await self._client.aclose()
            self._client = None

    # --------------------------------------------------------------- contract
    @abc.abstractmethod
    async def fetch_projects(self) -> list[Project]:
        """Return the current open projects (newest first when possible)."""

    @abc.abstractmethod
    async def submit_bid(
        self,
        project: Project,
        proposal: Proposal,
        amount: int,
        delivery_days: int | None,
    ) -> SubmissionResult:
        """Post the bid. Must never raise for expected failures — return FAILED instead."""

    async def healthcheck(self) -> tuple[bool, str]:
        return True, "ok"

    # ---------------------------------------------------------------- helpers
    async def get_html(self, path: str, **kwargs: Any) -> str:
        client = await self.client()
        response = await client.get(path, **kwargs)
        if response.status_code >= 400:
            raise SourceError(f"GET {path} -> HTTP {response.status_code}")
        return response.text

    @staticmethod
    def fail(message: str, http_status: int | None = None) -> SubmissionResult:
        return SubmissionResult(status=SubmissionStatus.FAILED, message=message, http_status=http_status)

    @staticmethod
    def timer() -> float:
        return time.monotonic()
