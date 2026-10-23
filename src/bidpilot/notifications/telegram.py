"""Telegram notifier — Bot API over plain HTTP (no SDK dependency)."""

from __future__ import annotations

import logging

import httpx

from ..models import Event
from .base import Notifier
from .formatting import render_event

log = logging.getLogger(__name__)


class TelegramNotifier(Notifier):
    name = "telegram"

    def __init__(
        self,
        token: str | None,
        chat_ids: list[str],
        *,
        enabled: bool = True,
        send_full_proposal: bool = True,
        client: httpx.AsyncClient | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.token = token
        self.chat_ids = [str(c) for c in chat_ids if str(c).strip()]
        self.enabled = bool(enabled and token and self.chat_ids)
        self.send_full_proposal = send_full_proposal
        self._client = client
        self.timeout = timeout
        if not self.enabled:
            log.info("Telegram notifier disabled (missing token or chat ids)")

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def api_base(self) -> str:
        return f"https://api.telegram.org/bot{self.token}"

    async def send(self, event: Event) -> bool:
        if not self.enabled:
            return False
        text = render_event(event, full_proposal=self.send_full_proposal)
        client = await self._get_client()
        all_ok = True
        for chat_id in self.chat_ids:
            for chunk in split_message(text, 3_900):
                try:
                    response = await client.post(
                        f"{self.api_base}/sendMessage",
                        json={
                            "chat_id": chat_id,
                            "text": chunk,
                            "parse_mode": "HTML",
                            "disable_web_page_preview": True,
                        },
                    )
                except httpx.HTTPError as exc:
                    log.error("Telegram send failed: %s", exc)
                    all_ok = False
                    continue
                if response.status_code >= 400:
                    log.error("Telegram HTTP %s: %s", response.status_code, response.text[:300])
                    all_ok = False
        return all_ok

    async def send_text(self, chat_id: str, text: str) -> bool:
        """Used by the CLI (`bidpilot ping`) and the /start helper."""
        if not self.token:
            return False
        client = await self._get_client()
        response = await client.post(
            f"{self.api_base}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        )
        return response.status_code < 400

    async def get_updates(self) -> list[dict]:
        """Helper for discovering your chat_id (`bidpilot telegram-whoami`)."""
        if not self.token:
            return []
        client = await self._get_client()
        response = await client.get(f"{self.api_base}/getUpdates")
        if response.status_code >= 400:
            return []
        return response.json().get("result", [])


def split_message(text: str, limit: int = 3_900) -> list[str]:
    """Split long messages on paragraph boundaries, respecting Telegram's 4096 limit."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n"):
        candidate = f"{current}\n{paragraph}" if current else paragraph
        if len(candidate) > limit:
            if current:
                chunks.append(current)
            while len(paragraph) > limit:
                chunks.append(paragraph[:limit])
                paragraph = paragraph[limit:]
            current = paragraph
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks
