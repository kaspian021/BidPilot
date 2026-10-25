"""Project filtering — cheap, explainable rules that run before any LLM call.

Two questions matter more than any keyword list:

* does the posting state a budget? (otherwise the price is a guess)
* does the posting mention something this user can actually do?

Both are on by default and the skill vocabulary comes from the user's own
profile (``profile.stack`` + ``profile.role``), so «طراحی لوگو» or «تدوین ریلز»
never reaches the proposal generator for a Flutter/.NET developer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import FiltersConfig
from .models import Project
from .text import has_any

#: role words that are too generic to prove a project matches a skill
_STOPWORDS = {
    "برنامه",
    "نویس",
    "نویسی",
    "برنامه‌نویس",
    "developer",
    "engineer",
    "مهندس",
    "متخصص",
    "کارشناس",
    "فول",
    "استک",
    "full",
    "stack",
    "and",
    "the",
    "با",
    "و",
}

#: Persian equivalents that should also match, e.g. "موبایل" for "Flutter"
_SKILL_ALIASES: dict[str, tuple[str, ...]] = {
    "flutter": ("فلاتر", "اپلیکیشن موبایل", "اپ موبایل"),
    "react native": ("ری‌اکت نیتیو", "اپلیکیشن موبایل"),
    "unity": ("یونیتی", "بازی"),
    "asp.net": ("ای اس پی", "دات نت", "dotnet", "asp net"),
    "asp.net core": ("asp.net", ".net", "دات نت", "dotnet", "asp net"),
    ".net": ("دات نت", "dotnet"),
    "c#": ("سی شارپ", "c sharp"),
    "wordpress": ("وردپرس",),
    "woocommerce": ("ووکامرس",),
    "laravel": ("لاراول",),
    "python": ("پایتون",),
    "django": ("جنگو",),
    "fastapi": ("فست ای پی آی",),
    "react": ("ری اکت",),
    "node.js": ("نود", "nodejs"),
    "javascript": ("جاوا اسکریپت",),
    "typescript": ("تایپ اسکریپت",),
    "postgresql": ("پستگرس", "postgres"),
    "mysql": ("مای اس کیو ال",),
    "docker": ("داکر",),
    "ui/ux": ("یو آی", "طراحی رابط کاربری", "طراحی ui"),
    "php": ("پی اچ پی",),
    "java": ("جاوا",),
    "android": ("اندروید",),
    "ios": ("آی او اس",),
    "seo": ("سئو",),
}


@dataclass
class FilterDecision:
    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:  # convenience: `if decision:`
        return self.allowed


def normalise_keyword(value: str) -> str:
    text = str(value or "").strip().lower()
    text = text.replace("‌", " ").replace("ي", "ی").replace("ك", "ک")
    return re.sub(r"\s+", " ", text)


def derive_skill_keywords(stack: list[str], role: str = "") -> list[str]:
    """Extract concrete skills from stack items and compound job titles."""
    words: list[str] = []

    def add_keyword(value: str) -> None:
        cleaned = normalise_keyword(value)
        if not cleaned or len(cleaned) < 2 or cleaned in _STOPWORDS:
            return
        if cleaned not in words:
            words.append(cleaned)

    for item in stack:
        for chunk in re.split(r"[,،/|+]", str(item or "")):
            add_keyword(chunk)

    # Roles often combine a generic job title with the actual technologies,
    # e.g. "مهندس Flutter و .NET". Split conjunctions and strip leading/trailing
    # role words so ".NET" and "Flutter" can match project skill tags directly.
    for chunk in re.split(r"[,،/|+]", str(role or "")):
        for part in re.split(r"\s+(?:و|and)\s+", chunk, flags=re.IGNORECASE):
            tokens = normalise_keyword(part).split()
            while tokens and tokens[0] in _STOPWORDS:
                tokens.pop(0)
            while tokens and tokens[-1] in _STOPWORDS:
                tokens.pop()
            add_keyword(" ".join(tokens))

    return words


def _matches(haystack: str, keyword: str) -> bool:
    needle = normalise_keyword(keyword)
    if not needle:
        return False
    if needle in haystack:
        return True
    for alias in _SKILL_ALIASES.get(needle, ()):
        if normalise_keyword(alias) in haystack:
            return True
    return False


class ProjectFilter:
    def __init__(
        self,
        config: FiltersConfig,
        *,
        profile_skills: list[str] | None = None,
        profile_role: str = "",
    ) -> None:
        self.config = config
        configured = [normalise_keyword(k) for k in (config.skill_keywords or []) if str(k).strip()]
        self.skill_keywords = configured or derive_skill_keywords(profile_skills or [], profile_role)

    # ------------------------------------------------------------------ rules
    def evaluate(self, project: Project) -> FilterDecision:
        cfg = self.config
        haystack = normalise_keyword(
            f"{project.title}\n{project.description}\n{' '.join(project.skills)}\n{project.category or ''}"
        )

        if cfg.include_keywords and not has_any(haystack, cfg.include_keywords):
            return FilterDecision(False, "هیچ‌کدام از کلیدواژه‌های مورد علاقه در پروژه نیست.")

        if cfg.exclude_keywords and has_any(haystack, cfg.exclude_keywords):
            matched = [kw for kw in cfg.exclude_keywords if has_any(haystack, [kw])]
            return FilterDecision(False, f"کلیدواژه‌ی ممنوعه پیدا شد: {'، '.join(matched[:3])}")

        if cfg.categories_allow:
            if not project.category or not has_any(project.category, cfg.categories_allow):
                return FilterDecision(False, f"دسته‌بندی {project.category or 'نامشخص'} در لیست مجاز نیست.")

        budget = project.budget
        known = bool(budget and budget.is_known)

        if cfg.require_budget and not known:
            return FilterDecision(False, "بودجه‌ی پروژه اعلام نشده است؛ بدون بودجه قیمت‌گذاری نمی‌کنم.")

        if known:
            assert budget is not None
            low = budget.lower or 0
            high = budget.upper or low
            if cfg.min_budget is not None and high < cfg.min_budget:
                return FilterDecision(False, "بودجه‌ی پروژه از حداقل تعیین‌شده کمتر است.")
            if cfg.max_budget is not None and low > cfg.max_budget:
                return FilterDecision(False, "بودجه‌ی پروژه از حداکثر تعیین‌شده بیشتر است.")
            if cfg.skip_negotiable and budget.negotiable:
                return FilterDecision(False, "بودجه توافقی است و رد کردن توافقی فعال شده.")

        if cfg.require_skill_match:
            if not self.skill_keywords:
                return FilterDecision(
                    False,
                    "فهرست مهارت‌های پروفایل خالی است؛ در /profile مهارت‌ها را کامل کن.",
                )
            if not any(_matches(haystack, keyword) for keyword in self.skill_keywords):
                return FilterDecision(
                    False,
                    "هیچ‌کدام از مهارت‌های پروفایل در این پروژه دیده نشد.",
                )

        return FilterDecision(True)

    # ------------------------------------------------------------------ view
    def describe_keywords(self, limit: int = 12) -> str:
        if not self.skill_keywords:
            return "—"
        head = "، ".join(self.skill_keywords[:limit])
        extra = len(self.skill_keywords) - limit
        return head + (f" و {extra} مورد دیگر" if extra > 0 else "")
