"""Proposal generation: LLM first, deterministic template as fallback, validated always."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from ..config import AppConfig, LLMConfig, Profile, ProposalConfig
from ..llm_providers import LLMProvider, build_client, resolve_chain
from ..models import Project, Proposal
from ..pricing.budget import format_toman
from ..text import count_emoji, persian_ratio, to_persian_digits, truncate
from .llm_client import LLMClient, LLMError
from .playbook import insights_for
from .prompts import REPAIR_PROMPT, build_prompts

log = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).with_name("templates")
_MARKDOWN_MARKERS = ("**", "##", "```", "<b>", "<br>")
_SALAM = ("سلام", "وقت بخیر", "درود")


class ProposalError(RuntimeError):
    pass


@dataclass
class ValidationReport:
    issues: list[str]

    @property
    def ok(self) -> bool:
        return not self.issues


def validate_proposal(
    text: str,
    config: ProposalConfig,
    amount: int | None = None,
    *,
    strict_length: bool = True,
) -> ValidationReport:
    issues: list[str] = []
    stripped = (text or "").strip()

    if not stripped:
        return ValidationReport(["متن خالی است."])
    if not stripped.startswith(_SALAM):
        issues.append("متن باید با سلام/وقت بخیر شروع شود.")
    if strict_length and len(stripped) < config.min_chars:
        issues.append(f"متن خیلی کوتاه است ({len(stripped)} < {config.min_chars}).")
    if len(stripped) > config.max_chars:
        issues.append(f"متن خیلی بلند است ({len(stripped)} > {config.max_chars}).")
    if persian_ratio(stripped) < 0.55:
        issues.append("نسبت فارسی متن کم است؛ متن باید فارسی روان باشد.")
    if count_emoji(stripped) > config.max_emoji:
        issues.append(f"ایموجی زیاد است (حداکثر {config.max_emoji}).")
    for marker in _MARKDOWN_MARKERS:
        if marker in stripped:
            issues.append(f"مارک‌داون/HTML مجاز نیست ({marker}).")
    for phrase in config.banned_phrases:
        if phrase and phrase in stripped:
            issues.append(f"عبارت ممنوعه استفاده شده: {phrase}")
    if amount is not None:
        # accept ۱٬۰۰۰٬۰۰۰ / ۱,۰۰۰,۰۰۰ / 1000000 in either digit script
        needles = {
            f"{amount:,}",
            to_persian_digits(f"{amount:,}"),
            f"{amount:,}".replace(",", "٬"),
            to_persian_digits(f"{amount:,}".replace(",", "٬")),
            str(amount),
            to_persian_digits(str(amount)),
        }
        if not any(needle in stripped for needle in needles):
            issues.append(f"قیمت پیشنهادی ({amount:,}) در متن ذکر نشده است.")
    if "تومان" not in stripped and "تومن" not in stripped:
        issues.append("واحد قیمت (تومان) در متن نیامده است.")
    return ValidationReport(issues)


class ProposalGenerator:
    def __init__(
        self,
        config: AppConfig,
        *,
        llm: LLMClient | None = None,
        llm_api_key: str | None = None,
        llm_keys: dict[str, str] | None = None,
    ) -> None:
        self.app = config
        self.settings: ProposalConfig = config.proposal
        self.profile: Profile = config.profile
        self.llm_config: LLMConfig = config.llm
        self._llm = llm
        self._llm_api_key = llm_api_key
        self._llm_keys = dict(llm_keys or {})
        self._clients: dict[str, LLMClient] = {}
        self._env = Environment(
            loader=FileSystemLoader(str(_TEMPLATE_DIR)),
            autoescape=select_autoescape(enabled_extensions=()),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    # ------------------------------------------------------------------ llm
    @property
    def providers(self) -> list[LLMProvider]:
        """Primary free provider first, then the configured fallbacks (keys only)."""
        if self._llm is not None:
            return [
                LLMProvider(
                    id="injected",
                    base_url=self.llm_config.base_url,
                    model=self.llm_config.model,
                )
            ]
        return resolve_chain(self.llm_config, self._llm_keys, self._llm_api_key)

    @property
    def llm_ready(self) -> bool:
        return bool(self.llm_config.enabled and self.providers)

    def _clients_ready(self) -> list[tuple[LLMProvider, LLMClient]]:
        if self._llm is not None:
            return [(self.providers[0], self._llm)]
        ready: list[tuple[LLMProvider, LLMClient]] = []
        for provider in self.providers:
            client = self._clients.get(provider.id)
            if client is None:
                client = build_client(provider, self.llm_config)
                self._clients[provider.id] = client
            ready.append((provider, client))
        return ready

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()
        if self._llm is not None:
            await self._llm.aclose()

    # --------------------------------------------------------------- public
    async def generate(
        self,
        project: Project,
        amount: int,
        delivery_days: int | None = None,
        *,
        force_template: bool = False,
    ) -> Proposal:
        if not force_template and self.llm_ready:
            try:
                return await self._generate_with_llm(project, amount, delivery_days)
            except LLMError as exc:
                log.warning("LLM generation failed (%s); falling back", exc)
                if self.settings.on_generation_failure == "skip":
                    raise ProposalError(f"تولید پیشنهاد با LLM ناموفق بود: {exc}") from exc
        return self.generate_from_template(project, amount, delivery_days)

    async def _generate_with_llm(self, project: Project, amount: int, delivery_days: int | None) -> Proposal:
        """Try the free providers in order; the first valid text wins."""
        system, user = build_prompts(
            project,
            self.profile,
            self.settings,
            amount,
            delivery_days,
        )
        failures: list[str] = []
        for provider, client in self._clients_ready():
            try:
                response = await client.complete(system, user)
            except LLMError as exc:
                log.warning("Provider %s failed: %s", provider.id, exc)
                failures.append(f"{provider.id}: {exc}")
                continue

            text = response.text.strip()
            report = validate_proposal(text, self.settings, amount)
            if not report.ok:
                log.info(
                    "Proposal validation issues from %s: %s — asking for a repair pass",
                    provider.id,
                    report.issues,
                )
                try:
                    repair = REPAIR_PROMPT.format(issues="؛ ".join(report.issues), text=text)
                    fixed = await client.complete(system, repair)
                    if validate_proposal(fixed.text.strip(), self.settings, amount).ok:
                        text = fixed.text.strip()
                        response = fixed
                except LLMError as exc:
                    log.warning("Repair pass failed (%s): %s", provider.id, exc)

            final_report = validate_proposal(text, self.settings, amount)
            if final_report.ok:
                return Proposal(
                    text=text,
                    generated_by=f"llm:{response.model or provider.model}",
                    latency_ms=response.latency_ms,
                    usage=response.usage,
                )
            failures.append(f"{provider.id}: " + "؛ ".join(final_report.issues))

        detail = "؛ ".join(failures) if failures else "هیچ ارائه‌دهنده‌ای تنظیم نشده است."
        raise LLMError(f"تولید متن با مدل زبانی ناموفق بود — {detail}")

    def generate_from_template(self, project: Project, amount: int, delivery_days: int | None) -> Proposal:
        insights = insights_for(project, limit=3)
        challenges = "\n".join(f"• {challenge}؛ راه‌حل: {solution}" for challenge, solution in insights)

        relevance_bits: list[str] = []
        if project.skills:
            overlap = [s for s in project.skills if any(s.lower() in t.lower() for t in self.profile.stack)]
            if overlap:
                relevance_bits.append(
                    f"با ابزارهای مورد نیاز این پروژه ({'، '.join(overlap[:3])}) کار کرده‌ام"
                )
        if not relevance_bits:
            relevance_bits.append("پروژه‌هایی با همین جنس کار و همین سطح از جزئیات را اجرا کرده‌ام")
        relevance = "، و ".join(relevance_bits) + "."

        template = self._env.get_template("default_fa.j2")
        text = template.render(
            greeting=self.settings.greeting or "سلام و وقت بخیر",
            profile=self.profile,
            project=project,
            years_fa=to_persian_digits(str(self.profile.years_experience)),
            relevance=relevance,
            challenges=challenges,
            amount_fa=to_persian_digits(f"{amount:,}"),
            delivery_fa=to_persian_digits(f"{delivery_days} روز") if delivery_days else "توافقی",
            budget_fa=to_persian_digits(project.budget.as_text()) if project.budget else "توافقی",
            signature=self.profile.signature
            or f"با احترام، {self.profile.full_name}"
            + (f"\n{self.profile.portfolio_url}" if self.profile.portfolio_url else ""),
        )
        text = self._tidy(text)

        report = validate_proposal(text, self.settings, amount)
        if report.ok:
            return Proposal(text=text, generated_by="template", latency_ms=0)

        # Length problems from the template are fixable deterministically.
        if len(text) > self.settings.max_chars:
            text = self._shrink(text, self.settings.max_chars)
        report = validate_proposal(text, self.settings, amount, strict_length=False)
        if not report.ok:
            raise ProposalError("قالب پیشنهاد معتبر نیست: " + "؛ ".join(report.issues))
        return Proposal(text=text, generated_by="template", latency_ms=0)

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _tidy(text: str) -> str:
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]+\n", "\n", text)
        return text.strip()

    @staticmethod
    def _shrink(text: str, limit: int) -> str:
        """Drop the least important bullet lines until the text fits."""
        lines = text.split("\n")
        while len("\n".join(lines)) > limit:
            bullet_indexes = [i for i, line in enumerate(lines) if line.strip().startswith("•")]
            if len(bullet_indexes) <= 2:
                return truncate(text, limit)
            del lines[bullet_indexes[-1]]
        return "\n".join(lines)


__all__ = ["ProposalGenerator", "ProposalError", "validate_proposal", "format_toman"]
