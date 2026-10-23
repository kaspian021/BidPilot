"""Notifier contract + dispatcher (fan-out, failure tolerant)."""

from __future__ import annotations

import abc
import logging

from ..models import Event

log = logging.getLogger(__name__)


class Notifier(abc.ABC):
    name: str = "base"
    enabled: bool = True

    @abc.abstractmethod
    async def send(self, event: Event) -> bool:
        """Deliver the event. Return True on success; never raise for delivery errors."""

    async def aclose(self) -> None:  # pragma: no cover - optional hook
        return None


class NotifierDispatcher:
    def __init__(self, notifiers: list[Notifier]) -> None:
        self.notifiers = [n for n in notifiers if n.enabled]

    async def dispatch(self, event: Event) -> dict[str, bool]:
        results: dict[str, bool] = {}
        for notifier in self.notifiers:
            try:
                results[notifier.name] = await notifier.send(event)
            except Exception as exc:  # a broken channel must not stop bidding
                log.error("Notifier %s failed: %s", notifier.name, exc)
                results[notifier.name] = False
        return results

    async def aclose(self) -> None:
        for notifier in self.notifiers:
            try:
                await notifier.aclose()
            except Exception:  # pragma: no cover
                pass
