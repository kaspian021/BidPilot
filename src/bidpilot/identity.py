"""Canonical identity — the user's name is data, never model output.

The complaint this module fixes: every generated bid introduced the user with
a *new* rendering of their name — a typo one time, Persian the next, English
transliteration after that.  The name comes from the user's own resume /
portfolio, so it must be copied **character for character** in every proposal:
no translation, no transliteration, no "close enough".

Design (deterministic, not prompt-only):

* :func:`canonical_identity` is the single source: ``full_name`` /
  ``first_name`` exactly as stored (only outer whitespace is trimmed).
* :func:`enforce_identity` rewrites a finished proposal: the introduction's
  name is replaced by the exact ``first_name`` and the closing signature by
  the exact canonical signature.  Whatever the model wrote there is discarded.
* :func:`identity_issues` lets validation *prove* the exact strings are
  present, so a text that still deviates never reaches the marketplace.
* :func:`lock_extracted_name` does the same job at import time: a name the
  extraction LLM returns is only trusted when it is found in the source text
  (snapped to the source's exact spelling); otherwise the name actually
  present in the source wins, and if none is found the hallucinated name is
  dropped instead of stored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Folded comparison only (never stored): Arabic ي/ك, ZWNJ, tatweel and
# diacritics count as "same letter" when *finding* a name, so a model that
# returned «محمد» with Arabic ي is snapped back to the resume's exact «محمد».
_FOLD = str.maketrans({"ي": "ی", "ك": "ک", "ة": "ه", "ؤ": "و", "إ": "ا", "أ": "ا", "ٰ": "", "ـ": "", "‌": " ", "‏": ""})
_DIACRITICS = re.compile(r"[\u064b-\u0652\u0670]")
_INTRO_RE = re.compile(r"من\s+([^\n،,.؛:]{1,60}?)\s+هستم")
_SIGNATURE_MARKERS = ("با احترام", "با سپاس", "ارادتمند")
_ROLE_WORDS = (
    "برنامه‌نویس", "برنامه نویس", "توسعه‌دهنده", "توسعه دهنده", "مهندس",
    "طراح", "فریلنسر", "متخصص", "مشاور", "developer", "engineer", "designer",
)


def fold_name(value: str) -> str:
    """Normalise a name for *comparison* (the stored name is never folded)."""
    text = _DIACRITICS.sub("", str(value or "").translate(_FOLD))
    return re.sub(r"\s+", " ", text).strip().casefold()


def _clean(value: str) -> str:
    return re.sub(r"[ \t]+", " ", str(value or "")).strip()


@dataclass(frozen=True)
class Identity:
    first_name: str
    full_name: str

    @property
    def usable(self) -> bool:
        from .config import Profile

        placeholders = {fold_name(p) for p in Profile.PLACEHOLDERS}
        return bool(self.full_name) and fold_name(self.full_name) not in placeholders


def canonical_identity(profile) -> Identity:
    """The user's name exactly as stored — the only spelling ever emitted."""
    full = _clean(getattr(profile, "full_name", "") or "")
    first = _clean(getattr(profile, "first_name", "") or "")
    if full and not first:
        first = full.split(" ")[0]
    if first and not full:
        full = first
    return Identity(first_name=first, full_name=full)


def canonical_signature(profile) -> str:
    """The exact closing block: an existing signature is kept only when it
    already carries the exact name; a stale one (another name, another
    script) is rebuilt from the canonical name."""
    ident = canonical_identity(profile)
    raw = str(getattr(profile, "signature", "") or "").strip()
    if raw and ident.full_name:
        lines = [line.rstrip() for line in raw.splitlines()]
        snapped = False
        for i, line in enumerate(lines):
            if fold_name(line) in {fold_name(ident.full_name), fold_name(ident.first_name)}:
                lines[i] = ident.full_name
                snapped = True
        if snapped:
            return "\n".join(lines).strip()
        if ident.full_name in raw:  # name inside a longer line, spelled exactly
            return raw
        # signature names somebody else (or another script): never sign with it
    signature = f"با احترام،\n{ident.full_name}" if ident.full_name else raw
    portfolio = str(getattr(profile, "portfolio_url", "") or "").strip()
    if portfolio and portfolio not in signature:
        signature = f"{signature}\n{portfolio}" if signature else portfolio
    return signature


def identity_issues(text: str, profile) -> list[str]:
    """Validation issues when the exact canonical name is not what is written."""
    ident = canonical_identity(profile)
    if not ident.usable:
        return []
    body = str(text or "")
    issues: list[str] = []
    if ident.full_name not in body:
        issues.append(
            f"نام کامل دقیقاً همان‌طور که در پروفایل ثبت شده («{ident.full_name}») در متن نیامده است؛ "
            "اسم نباید ترجمه، آوانویسی یا با املای دیگری نوشته شود."
        )
    intro = body[:400]
    if ident.first_name and ident.first_name not in intro:
        issues.append(f"معرفی باید با نام دقیق کاربر («{ident.first_name}») باشد.")
    return issues


def _fix_intro(text: str, profile, ident: Identity) -> str:
    if not ident.first_name:
        return text
    # search the opening (greeting + first paragraph) for «من … هستم»;
    # the signature at the end is not an introduction, so it is excluded
    sig_at = min((text.find(m) for m in _SIGNATURE_MARKERS if text.find(m) > 0), default=len(text))
    opening = text[: min(500, sig_at)]
    match = _INTRO_RE.search(opening)
    if match:
        captured = match.group(1).strip()
        if fold_name(captured) == fold_name(ident.full_name):
            replacement = f"من {ident.full_name} هستم"
        elif fold_name(captured) == fold_name(ident.first_name):
            replacement = f"من {ident.first_name} هستم"
        elif any(word in captured.casefold() for word in _ROLE_WORDS):
            replacement = f"من {ident.first_name}، {captured} هستم"
        else:  # some other name (typo / transliteration / invented): replace it
            replacement = f"من {ident.first_name} هستم"
        return text[: match.start()] + replacement + text[match.end():]
    if ident.first_name in opening:
        return text
    # no introduction at all: insert the canonical one after the greeting line
    intro_line = profile.intro_line() if hasattr(profile, "intro_line") else f"من {ident.first_name} هستم."
    lines = text.split("\n")
    insert_at = 1 if lines and lines[0].strip() else 0
    lines.insert(insert_at, f"\n{intro_line}")
    return "\n".join(lines)


def _fix_signature(text: str, profile) -> str:
    signature = canonical_signature(profile)
    if not signature:
        return text
    body = text.rstrip()
    idx = max(body.rfind(marker) for marker in _SIGNATURE_MARKERS)
    if idx > len(body) * 0.4:  # a real closing block, not a mid-text mention
        body = body[:idx].rstrip()
    return f"{body}\n\n{signature}"


def enforce_identity(text: str, profile) -> str:
    """Return ``text`` with the introduction and signature forced to the
    canonical name.  Deterministic: the model cannot misspell what it no
    longer writes."""
    ident = canonical_identity(profile)
    if not ident.usable:
        return text
    fixed = _fix_signature(str(text or ""), profile)
    fixed = _fix_intro(fixed, profile, ident)
    return fixed


def lock_extracted_name(fields: dict, source_text: str) -> dict:
    """Snap an extracted name to the exact spelling in the source text.

    * extracted name found in the source (folded comparison) → replaced by
      the source's exact substring;
    * not found → the name the deterministic extractor finds in the source
      wins (the LLM's rendering — e.g. a transliteration — is discarded);
    * no name in the source at all → the extracted name is dropped rather
      than stored on a guess.
    """
    out = dict(fields or {})
    full = _clean(str(out.get("full_name") or ""))
    if not full:
        return out
    source = str(source_text or "")
    exact = _find_exact(full, source)
    if exact is None:
        try:
            from .profile_import import heuristic_extract

            candidate = _clean(str(heuristic_extract(source).get("full_name") or ""))
        except Exception:  # noqa: BLE001 - import must never fail on this
            candidate = ""
        exact = candidate or None
    if exact is None:
        out.pop("full_name", None)
        out.pop("first_name", None)
        return out
    out["full_name"] = exact
    first = _clean(str(out.get("first_name") or ""))
    tokens = exact.split(" ")
    if not first or fold_name(first) not in {fold_name(t) for t in tokens}:
        out["first_name"] = tokens[0]
    else:  # keep the source's exact first token spelling
        out["first_name"] = next(t for t in tokens if fold_name(t) == fold_name(first))
    return out


def _find_exact(name: str, source: str) -> str | None:
    """The substring of ``source`` that is ``name`` (folded match, exact text)."""
    folded_name = fold_name(name)
    if not folded_name:
        return None
    for line in str(source or "").splitlines():
        if fold_name(line) == folded_name:
            return _clean(line)
    # name inside a longer line: match the folded word sequence
    words = folded_name.split(" ")
    for line in str(source or "").splitlines():
        tokens = _clean(line).split(" ")
        folded_tokens = [fold_name(t) for t in tokens]
        for i in range(0, max(0, len(tokens) - len(words) + 1)):
            if folded_tokens[i : i + len(words)] == words:
                return " ".join(tokens[i : i + len(words)])
    return None


__all__ = [
    "Identity",
    "canonical_identity",
    "canonical_signature",
    "enforce_identity",
    "fold_name",
    "identity_issues",
    "lock_extracted_name",
]
