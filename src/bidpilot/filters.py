"""Project filtering — cheap, explainable rules that run before any LLM call."""

from __future__ import annotations

from dataclasses import dataclass

from .config import FiltersConfig
from .models import Project
from .text import has_any


@dataclass
class FilterDecision:
    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:  # convenience: `if decision:`
        return self.allowed


class ProjectFilter:
    def __init__(self, config: FiltersConfig) -> None:
        self.config = config

    def evaluate(self, project: Project) -> FilterDecision:
        cfg = self.config
        haystack = (
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
        if budget and budget.is_known:
            low = budget.lower or 0
            high = budget.upper or low
            if cfg.min_budget is not None and high < cfg.min_budget:
                return FilterDecision(False, "بودجه‌ی پروژه از حداقل تعیین‌شده کمتر است.")
            if cfg.max_budget is not None and low > cfg.max_budget:
                return FilterDecision(False, "بودجه‌ی پروژه از حداکثر تعیین‌شده بیشتر است.")
            if cfg.skip_negotiable and budget.negotiable:
                return FilterDecision(False, "بودجه توافقی است و رد کردن توافقی فعال شده.")
        return FilterDecision(True)
