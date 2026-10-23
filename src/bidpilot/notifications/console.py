"""Console notifier — always on in development, prints a rendered event to stdout."""

from __future__ import annotations

from bs4 import BeautifulSoup

from ..models import Event
from .base import Notifier
from .formatting import render_event


class ConsoleNotifier(Notifier):
    name = "console"

    def __init__(self, enabled: bool = True, full_proposal: bool = True) -> None:
        self.enabled = enabled
        self.full_proposal = full_proposal

    async def send(self, event: Event) -> bool:
        if not self.enabled:
            return False
        html = render_event(event, full_proposal=self.full_proposal)
        print(BeautifulSoup(html, "lxml").get_text("\n"))
        return True
