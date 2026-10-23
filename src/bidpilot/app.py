"""Application wiring — the single place where config becomes live objects."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .config import AppConfig, Secrets, get_secrets
from .db import Database
from .filters import ProjectFilter
from .notifications import ConsoleNotifier, NotifierDispatcher, TelegramNotifier
from .pipeline import BidPipeline, RunReport
from .pricing.strategy import PricingConfig
from .proposals.generator import ProposalGenerator
from .ratelimit import RateLimiter
from .repositories import Repository
from .sources import Source, create_source
from .sources.base import SourceError

log = logging.getLogger(__name__)


@dataclass
class Application:
    config: AppConfig
    secrets: Secrets
    db: Database
    sources: dict[str, Source] = field(default_factory=dict)
    generator: ProposalGenerator | None = None
    notifiers: NotifierDispatcher | None = None

    # ------------------------------------------------------------- lifecycle
    @classmethod
    def build(cls, config: AppConfig, secrets: Secrets | None = None) -> Application:
        secrets = secrets or get_secrets()
        app = cls(config=config, secrets=secrets, db=Database(config.database_url))
        app.generator = ProposalGenerator(config, llm_api_key=secrets.llm_api_key)
        app.notifiers = app._build_notifiers()
        for source_cfg in config.sources:
            if not source_cfg.enabled:
                continue
            try:
                app.sources[source_cfg.type] = create_source(
                    source_cfg.type, source_cfg.settings, config, secrets
                )
            except KeyError as exc:
                log.error("Source %s is not registered: %s", source_cfg.type, exc)
        return app

    def _build_notifiers(self) -> NotifierDispatcher:
        notifiers = [ConsoleNotifier(enabled=self.config.notifications.enabled, full_proposal=False)]
        notifiers.append(
            TelegramNotifier(
                token=self.secrets.telegram_bot_token,
                chat_ids=self.config.notifications.chat_ids,
                enabled=self.config.notifications.enabled,
                send_full_proposal=self.config.notifications.send_full_proposal,
            )
        )
        return NotifierDispatcher(notifiers)

    async def setup(self) -> None:
        await self.db.init()
        for source in self.sources.values():
            if getattr(source, "name", "") == "parscoders":
                try:
                    await source.login()  # type: ignore[attr-defined]
                except Exception as exc:  # pragma: no cover - network
                    log.warning("Login check failed: %s", exc)

    async def close(self) -> None:
        for source in self.sources.values():
            await source.aclose()
        if self.generator is not None:
            await self.generator.aclose()
        if self.notifiers is not None:
            await self.notifiers.aclose()
        await self.db.dispose()

    # ---------------------------------------------------------------- running
    async def cycle(
        self,
        source_names: list[str] | None = None,
        *,
        live: bool | None = None,
        max_projects: int | None = None,
    ) -> list[RunReport]:
        """One poll cycle with a fresh DB session (safe for long-running daemons)."""
        session = self.db.session()
        repo = Repository(session)
        pipeline = BidPipeline(
            config=self.config,
            repo=repo,
            generator=self.generator,  # type: ignore[arg-type]
            notifiers=self.notifiers,  # type: ignore[arg-type]
            rate_limiter=RateLimiter(self.config.runtime, repo),
            sources=self.sources,
            pricing=PricingConfig(**self.config.pricing).build(),
            live=live,
            max_projects=max_projects,
        )
        try:
            return await pipeline.run_once(source_names)
        finally:
            await session.close()

    async def healthcheck(self) -> dict[str, tuple[bool, str]]:
        results: dict[str, tuple[bool, str]] = {}
        for name, source in self.sources.items():
            try:
                results[name] = await source.healthcheck()
            except (SourceError, Exception) as exc:
                results[name] = (False, str(exc))
        return results

    def filter_preview(self):
        return ProjectFilter(self.config.filters)


def build_application(config: AppConfig, secrets: Secrets | None = None) -> Application:
    return Application.build(config, secrets)


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
