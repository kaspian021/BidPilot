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
from ..text import count_emoji, persian_ratio, to_ascii_digits, to_persian_digits
from .llm_client import LLMClient, LLMError, LLMResponse
from .playbook import insights_for
from .prompts import REPAIR_PROMPT, build_prompts

log = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).with_name("templates")
_MARKDOWN_MARKERS = ("**", "##", "```", "<b>", "<br>")
_SALAM = ("سلام", "وقت بخیر", "درود")


class ProposalError(RuntimeError):
    pass


_NUMBER_RUN = re.compile(r"\d[\d,٬،.\s]*\d|\d")
_SCALED = re.compile(r"(\d+(?:\.\d+)?)\s*(میلیون|هزار)")
_CURRENCY = re.compile(r"توم[ا]?ن|تومن")


def mentions_amount(text: str, amount: int) -> bool:
    """Does the bid actually state this price — in *any* digit/separator style?

    Models happily write «۱٬۵۰۰٬۰۰۰», «۱,۵۰۰,۰۰۰» or «۱.۵ میلیون». Comparing
    against one string made good proposals fail validation and fall back to a
    template, which looked like "the AI ignored the price".
    """
    ascii_text = to_ascii_digits(text or "").replace("٫", ".").replace("٬", ",")
    for match in _NUMBER_RUN.finditer(ascii_text):
        digits = re.sub(r"\D", "", match.group())
        if digits and int(digits) == amount:
            return True
    for match in _SCALED.finditer(ascii_text):
        value = float(match.group(1))
        scale = 1_000_000 if match.group(2) == "میلیون" else 1_000
        if abs(value * scale - amount) < max(1.0, amount * 0.005):
            return True
    return False


def mentions_currency(text: str) -> bool:
    return bool(_CURRENCY.search(to_ascii_digits(text or "")))


@dataclass
class ValidationReport:
    issues: list[str]

    @property
    def ok(self) -> bool:
        return not self.issues


#: Phrases that betray an LLM *refusal* instead of a proposal. Production
#: incident: for a "game cheat" project the model answered with an ethics
#: refusal that passed repair + validation and was one request away from
#: being submitted to the employer. Refusals are never repaired and never
#: template-fallback-ed — the project itself is skipped.
REFUSAL_MARKERS = (
    "نمی‌توانم همکاری",
    "نمی‌توانم در این پروژه",
    "نمی‌توانم پیشنهاد",
    "نمی‌توانم کمکی",
    "نمی‌توانم انجام",
    "نمی‌تونم همکاری",
    "امکان همکاری در این پروژه",
    "مخالف اخلاقیات",
    "مخالف قوانین بازی",
    "برخلاف قوانین",
    "برخلاف اخلاقیات",
    "نمی‌تواند در این پروژه",
    "i can't help",
    "i cannot help",
    "i can't assist",
    "i cannot assist",
    "against my guidelines",
    "against the game's rules",
)


def looks_like_refusal(text: str) -> bool:
    """True when the text contains an LLM refusal marker."""
    folded = (text or "").casefold().replace("‌", " ").replace("ـ", "")
    return any(marker.casefold().replace("‌", " ") in folded for marker in REFUSAL_MARKERS)


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
    if looks_like_refusal(stripped):
        return ValidationReport(["خروجی مدل ردّ همکاری (refusal) است، نه متن پیشنهاد؛ نباید ارسال شود."])
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
    if amount is not None and not mentions_amount(stripped, amount):
        issues.append(f"قیمت پیشنهادی ({amount:,}) در متن ذکر نشده است.")
    if not mentions_currency(stripped):
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
        #: why the last LLM attempt failed (empty when it worked or was not used)
        self.last_llm_error: str = ""
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
        self.last_llm_error = ""
        if not force_template:
            if not self.llm_ready:
                # ``enabled: false`` is a deliberate choice, not a failure
                if self.llm_config.enabled:
                    self.last_llm_error = "برای هوش مصنوعی کلید یا مدلی تنظیم نشده است."
            else:
                try:
                    return await self._generate_with_llm(project, amount, delivery_days)
                except LLMError as exc:
                    log.warning("LLM generation failed (%s); falling back", exc)
                    # never swallow it: the bot shows this text verbatim
                    self.last_llm_error = str(exc)
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
                response = await self._complete_proposal(client, system, user)
            except LLMError as exc:
                log.warning("Provider %s failed: %s", provider.id, exc)
                failures.append(f"{provider.id}: {exc}")
                continue

            text = response.text.strip()
            if looks_like_refusal(text):
                # A refusal means the model refused the *project* itself
                # (e.g. game cheats). Never repair it — a repaired refusal
                # with a greeting on top is what nearly got submitted — and
                # never template-fallback it: the project is skipped outright.
                log.error(
                    "[PROPOSAL][REFUSAL] provider=%s project=%r skipped",
                    provider.id,
                    project.title[:70],
                )
                raise ProposalError(
                    "مدل زبانی از همکاری روی این پروژه خودداری کرد (refusal)؛ پروژه رد می‌شود."
                )
            report = validate_proposal(text, self.settings, amount)
            if not report.ok:
                log.info(
                    "Proposal validation issues from %s: %s — asking for a repair pass",
                    provider.id,
                    report.issues,
                )
                try:
                    repair = REPAIR_PROMPT.format(issues="؛ ".join(report.issues), text=text)
                    fixed = await self._complete_proposal(client, system, repair)
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

    async def _complete_proposal(self, client: LLMClient, system: str, user: str) -> LLMResponse:
        """One proposal call — with more room when the answer was cut off.

        A truncated bid loses its closing paragraph (price + «تومان»), which
        validation then rejects as if the model had ignored the price. So a
        ``finish_reason == "length"`` answer is retried with twice the budget.
        """
        budget = max(int(self.llm_config.max_tokens or 900), 1_400)
        response = await self._call(client, system, user, budget)
        if getattr(response, "truncated", False):
            log.info("Proposal hit the %s-token limit; retrying with more room", budget)
            response = await self._call(client, system, user, min(budget * 2, 3_000))
        return response

    @staticmethod
    async def _call(client: LLMClient, system: str, user: str, budget: int) -> LLMResponse:
        try:
            return await client.complete(system, user, max_tokens=budget)
        except TypeError:  # a minimal client (tests, older wrappers) has no such kwarg
            return await client.complete(system, user)

    def generate_from_template(self, project: Project, amount: int, delivery_days: int | None) -> Proposal:
        """Render the longest template that fits ``proposal.max_chars``.

        The price sentence and the signature are *never* cut: they are what makes
        a bid valid. When the full template does not fit we fall back to the
        compact one, and only then drop extra bullet points. (Truncating the
        rendered text used to remove the closing paragraph entirely, so every
        template bid failed validation with "قیمت پیشنهادی … ذکر نشده است".)
        """
        insights = insights_for(project, limit=3)
        challenges = "\n".join(f"• {challenge}؛ راه‌حل: {solution}" for challenge, solution in insights)
        first_point, first_solution = (
            insights[0]
            if insights
            else (
                "شفاف بودن دامنه‌ی کار",
                "تحویل مرحله‌ای با بازبینی در هر مرحله",
            )
        )

        relevance_bits: list[str] = []
        if project.skills:
            overlap = [s for s in project.skills if any(s.lower() in t.lower() for t in self.profile.stack)]
            if overlap:
                relevance_bits.append(
                    f"با ابزارهای مورد نیاز این پروژه ({'، '.join(overlap[:3])}) کار کرده‌ام"
                )
        if not relevance_bits:
            # Never imply matching past experience unless the profile supports it.
            relevance_bits.append("راهکار را بر اساس نیازها و محدودیت‌های اعلام‌شده‌ی همین پروژه تنظیم می‌کنم")
        relevance = "، و ".join(relevance_bits) + "."

        context = {
            "greeting": self.settings.greeting or "سلام و وقت بخیر",
            "profile": self.profile,
            "project": project,
            "years_fa": to_persian_digits(str(self.profile.years_experience)),
            "relevance": relevance,
            "challenges": challenges,
            "first_point": first_point,
            "first_solution": first_solution,
            "amount_fa": to_persian_digits(f"{amount:,}"),
            "delivery_fa": to_persian_digits(f"{delivery_days} روز") if delivery_days else "توافقی",
            "budget_fa": to_persian_digits(project.budget.as_text()) if project.budget else "توافقی",
            "signature": self.profile.signature
            or f"با احترام، {self.profile.full_name}"
            + (f"\n{self.profile.portfolio_url}" if self.profile.portfolio_url else ""),
        }

        limit = int(self.settings.max_chars)
        full_text = self._tidy(self._env.get_template("default_fa.j2").render(**context))
        compact_text = self._tidy(self._env.get_template("compact_fa.j2").render(**context))

        # 1) the full text, 2) the same text with extra bullets removed (the
        # price paragraph and the signature always stay), 3) the compact text.
        for candidate in (full_text, *self._bullet_trimmed(full_text, limit), compact_text):
            if not candidate or len(candidate) > limit:
                continue
            if validate_proposal(candidate, self.settings, amount, strict_length=False).ok:
                return Proposal(text=candidate, generated_by="template", latency_ms=0)

        report = validate_proposal(compact_text, self.settings, amount, strict_length=False)
        if not report.ok:
            raise ProposalError("قالب پیشنهاد معتبر نیست: " + "؛ ".join(report.issues))
        raise ProposalError(
            f"متن پیشنهادی از سقف {limit} کاراکتر بلندتر است؛ "
            "در /style مقدار «حداکثر طول» را بزرگ‌تر کن (حداقل ۷۰۰)."
        )

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _tidy(text: str) -> str:
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]+\n", "\n", text)
        return text.strip()

    @staticmethod
    def _bullet_trimmed(text: str, limit: int) -> list[str]:
        """Progressively shorter versions of ``text`` (one bullet fewer at a time).

        The closing paragraph (price + delivery) and the signature are the parts
        validation needs, so they are never cut — only bullet lines are removed.
        """
        lines = text.split("\n")
        versions: list[str] = []
        while True:
            bullets = [i for i, line in enumerate(lines) if line.strip().startswith("•")]
            if len(bullets) <= 1:
                return versions
            del lines[bullets[-1]]
            candidate = "\n".join(lines)
            if len(candidate) <= limit:
                versions.append(candidate)


__all__ = ["ProposalGenerator", "ProposalError", "validate_proposal", "format_toman"]
