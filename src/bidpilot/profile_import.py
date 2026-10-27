"""Turn a resume / portfolio / GitHub profile into BidPilot's profile fields.

The wizard used to ask eight questions (name, role, years, stack, highlights,
portfolio, signature…). That is busywork: the user already has this information
in a PDF resume or on a page. So the bot accepts **one** input —

* a PDF resume (sent as a Telegram document, or a local path in the terminal),
* a portfolio/website URL,
* a GitHub profile URL,

— extracts the text and asks the *same* free LLM key the proposals use to turn
it into structured profile fields. No key / no network? A deterministic
heuristic extractor still fills in what it can, and the user can edit anything
afterwards with ``/profile``.

Nothing is invented: the prompt forbids guessing, and every extracted field is
shown back to the user before it is used.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from .jsonparse import parse_json_object
from .llm_providers import LLMProvider, resolve_chain
from .settings_store import SettingsStore

log = logging.getLogger(__name__)

#: hard cap so a 60-page PDF never blows up the prompt (or the rate limit)
MAX_TEXT_CHARS = 24_000
MAX_HIGHLIGHTS = 6
MAX_STACK = 20

#: profile fields the importer is allowed to write
PROFILE_FIELDS = (
    "first_name",
    "full_name",
    "role",
    "years_experience",
    "stack",
    "highlights",
    "portfolio_url",
)

#: technologies recognised by the heuristic extractor (canonical spelling kept)
STACK_VOCABULARY: tuple[str, ...] = (
    "Python",
    "Django",
    "FastAPI",
    "Flask",
    "DRF",
    "Node.js",
    "Express",
    "NestJS",
    "TypeScript",
    "JavaScript",
    "React",
    "Next.js",
    "Vue",
    "Nuxt",
    "Angular",
    "Svelte",
    "React Native",
    "Flutter",
    "Dart",
    "Swift",
    "Kotlin",
    "Java",
    "Spring Boot",
    "C#",
    "ASP.NET",
    ".NET",
    "Unity",
    "Blazor",
    "WPF",
    "PHP",
    "Laravel",
    "WordPress",
    "WooCommerce",
    "Magento",
    "SQL",
    "PostgreSQL",
    "MySQL",
    "SQL Server",
    "MongoDB",
    "Redis",
    "Elasticsearch",
    "Docker",
    "Kubernetes",
    "Linux",
    "Git",
    "CI/CD",
    "AWS",
    "Azure",
    "GCP",
    "Terraform",
    "GraphQL",
    "REST",
    "WebSocket",
    "Celery",
    "RabbitMQ",
    "Kafka",
    "Pandas",
    "NumPy",
    "TensorFlow",
    "PyTorch",
    "OpenCV",
    "Solidity",
    "Figma",
    "UI/UX",
    "SEO",
)

_ROLE_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("flutter", "برنامه‌نویس موبایل (Flutter)"),
    ("react native", "برنامه‌نویس موبایل (React Native)"),
    ("unity", "برنامه‌نویس بازی (Unity)"),
    ("asp.net", "برنامه‌نویس دات‌نت (ASP.NET)"),
    (".net", "برنامه‌نویس دات‌نت"),
    ("full stack", "برنامه‌نویس فول‌استک"),
    ("fullstack", "برنامه‌نویس فول‌استک"),
    ("front end", "برنامه‌نویس فرانت‌اند"),
    ("frontend", "برنامه‌نویس فرانت‌اند"),
    ("back end", "برنامه‌نویس بک‌اند"),
    ("backend", "برنامه‌نویس بک‌اند"),
    ("django", "برنامه‌نویس بک‌اند (Django)"),
    ("laravel", "برنامه‌نویس بک‌اند (Laravel)"),
    ("wordpress", "متخصص وردپرس"),
    ("mobile", "برنامه‌نویس موبایل"),
    ("android", "برنامه‌نویس اندروید"),
    ("ios", "برنامه‌نویس iOS"),
    ("devops", "مهندس DevOps"),
    ("data scien", "متخصص علم داده"),
    ("designer", "طراح UI/UX"),
)
_PERSIAN_ROLE_WORDS: tuple[tuple[str, str], ...] = (
    ("برنامه‌نویس", "برنامه‌نویس"),
    ("برنامه نویس", "برنامه‌نویس"),
    ("طراح", "طراح"),
    ("دواپس", "مهندس DevOps"),
    ("علم داده", "متخصص علم داده"),
)

_GITHUB_RE = re.compile(r"https?://(?:www\.)?github\.com/([A-Za-z0-9-]+)", re.I)
_URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.I)
_YEARS_RE = re.compile(r"(\d{1,2})\s*(?:\+|سال|years?|yrs?)", re.I)
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


class ProfileImportError(RuntimeError):
    """Persian, user-safe error message."""


class MissingDependency(ProfileImportError):
    """A package the importer needs is not installed (installable on the spot)."""

    def __init__(self, module: str, message: str) -> None:
        super().__init__(message)
        self.module = module


@dataclass
class ImportedProfile:
    """Result of one import — shown to the user before it is trusted."""

    kind: str
    fields: dict[str, Any] = field(default_factory=dict)
    used_llm: bool = False
    chars: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return bool(self.fields)

    def summary(self) -> str:
        return "; ".join(f"{key}={value}" for key, value in self.fields.items())


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------
def pdf_text(data: bytes, *, max_chars: int = MAX_TEXT_CHARS) -> str:
    """Extract selectable text from a PDF; raises on encrypted/scanned files."""
    if not data:
        raise ProfileImportError("فایل خالی است.")
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        from .deps import install_command

        raise MissingDependency(
            "pypdf",
            "بسته‌ی pypdf برای خواندن PDF نصب نیست.\n"
            f"دستور نصب برای همین پایتون:\n<code>{install_command('pypdf')}</code>",
        ) from exc

    try:
        reader = PdfReader(io.BytesIO(data))
        if getattr(reader, "is_encrypted", False):
            try:
                reader.decrypt("")
            except Exception as exc:  # noqa: BLE001 - pypdf errors vary
                raise ProfileImportError("فایل PDF رمزگذاری شده است؛ نسخه‌ی بدون رمز را بفرست.") from exc
        pages: list[str] = []
        for page in reader.pages[:40]:
            try:
                pages.append(page.extract_text() or "")
            except Exception as exc:  # noqa: BLE001 - one bad page must not kill the rest
                log.debug("PDF page skipped: %s", exc)
    except ProfileImportError:
        raise
    except Exception as exc:
        raise ProfileImportError(f"فایل PDF خوانده نشد: {exc}") from exc

    text = normalise_text("\n".join(pages))
    if len(text) < 40:
        raise ProfileImportError(
            "متنی داخل PDF پیدا نشد (احتمالاً اسکن‌شده یا تصویری است). "
            "یک PDF متنی بفرست یا لینک گیت‌هاب/پورتفولیو بده."
        )
    return text[:max_chars]


def normalise_text(text: str) -> str:
    text = str(text or "").translate(_DIGITS)
    text = text.replace("\u200c", " ").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def html_text(html: str, *, max_chars: int = MAX_TEXT_CHARS) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "head"]):
        tag.decompose()
    return normalise_text(soup.get_text("\n", strip=True))[:max_chars]


async def fetch_url_text(url: str, *, timeout: float = 25.0, proxy: str = "", store: SettingsStore | None = None, chat_id: str | int | None = None) -> str:
    """Read a public page (portfolio, GitHub profile) as plain text."""
    target = normalise_url(url)
    parsed = urlparse(target)
    if parsed.scheme not in {"http", "https"}:
        raise ProfileImportError("لینک باید با http:// یا https:// شروع شود.")

    # Resolve proxy: explicit > per-user > global
    resolved_proxy = (proxy or "").strip()
    if not resolved_proxy and store is not None:
        try:
            resolved_proxy = str(store.get("network.proxy") or store.get("llm.proxy") or "").strip()
        except Exception:
            pass
        if not resolved_proxy and chat_id is not None:
            try:
                from .per_user import get_user
                resolved_proxy = str(get_user(store, chat_id, "network.proxy") or "").strip()
            except Exception:
                pass

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 BidPilot",
        "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8",
    }
    client_kwargs: dict[str, object] = {"timeout": timeout, "follow_redirects": True, "headers": headers}
    if resolved_proxy:
        client_kwargs["proxy"] = resolved_proxy
    async with httpx.AsyncClient(**client_kwargs) as client:  # type: ignore[arg-type]
        try:
            if parsed.netloc.lower().endswith("github.com"):
                text = await _github_text(client, target)
                if text:
                    return text
            response = await client.get(target)
        except httpx.HTTPError as exc:
            detail = str(exc).strip() or f"{type(exc).__name__} بدون جزئیات برای {target}"
            raise ProfileImportError(f"لینک باز نشد: {detail}. آیا لینک عمومی است؟ اگر در ایران هستی با /proxy پروکسی (مثلاً http://127.0.0.1:10809) تنظیم کن.") from exc
        except Exception as exc:  # pragma: no cover
            detail = str(exc).strip() or f"{type(exc).__name__} برای {target}"
            raise ProfileImportError(f"لینک باز نشد: {detail}") from exc

    if response.status_code >= 400:
        if response.status_code in {403, 404}:
            raise ProfileImportError(f"صفحه با خطای HTTP {response.status_code} پاسخ داد (آدرس: {target}). لینک عمومی است؟ اگر در ایران هستی با /proxy پروکسی تنظیم کن.")
        raise ProfileImportError(f"صفحه با خطای HTTP {response.status_code} پاسخ داد (آدرس: {target}). لینک عمومی است؟")
    text = html_text(response.text)
    if len(text) < 30:
        raise ProfileImportError(
            "از این صفحه متنی استخراج نشد (احتمالاً جاوااسکریپتی است). "
            "لینک گیت‌هاب یا فایل PDF رزومه را امتحان کن."
        )
    return text


async def _github_text(client: httpx.AsyncClient, url: str) -> str:
    """Use the public GitHub API (no token) — far cleaner than scraping HTML."""
    match = _GITHUB_RE.match(url)
    if not match:
        return ""
    user = match.group(1)
    if user.lower() in {"orgs", "topics", "features", "about", "pricing", "login", "join"}:
        return ""

    parts: list[str] = []
    try:
        profile = await client.get(f"https://api.github.com/users/{user}")
        if profile.status_code == 200:
            data = profile.json()
            parts.append(f"login: {data.get('login', '')}")
            if data.get("name"):
                parts.append(f"name: {data['name']}")
            if data.get("bio"):
                parts.append(f"bio: {data['bio']}")
            if data.get("location"):
                parts.append(f"location: {data['location']}")
            if data.get("blog"):
                parts.append(f"website: {data['blog']}")
            parts.append(f"public_repos: {data.get('public_repos', 0)}")

        repos = await client.get(
            f"https://api.github.com/users/{user}/repos",
            params={"per_page": 30, "sort": "updated"},
        )
        if repos.status_code == 200:
            lines = []
            for repo in repos.json() or []:
                name = repo.get("name") or ""
                if repo.get("fork"):
                    continue
                desc = (repo.get("description") or "").strip()
                language = repo.get("language") or ""
                topics = ", ".join(repo.get("topics") or [])
                lines.append(f"- {name} [{language}] {desc} {topics}".strip())
            if lines:
                parts.append("repositories:\n" + "\n".join(lines[:30]))
    except (httpx.HTTPError, ValueError) as exc:
        log.debug("GitHub API failed: %s", exc)
        return ""

    if len(parts) <= 1:
        return ""
    parts.append(f"profile_url: {url}")
    return normalise_text("\n".join(parts))[:MAX_TEXT_CHARS]


def normalise_url(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        raise ProfileImportError("لینک خالی است.")
    if not re.match(r"^https?://", text, re.I):
        text = "https://" + text
    return text


# ---------------------------------------------------------------------------
# Extraction: LLM first, deterministic heuristics as the safety net
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """تو یک استخراج‌کننده‌ی دقیق اطلاعات حرفه‌ای هستی.
از متن داده‌شده (رزومه، پروفایل گیت‌هاب یا سایت شخصی) فقط اطلاعاتی را بردار که
واقعاً در متن آمده است. هیچ چیزی از خودت نساز، حدس نزن و بزرگ‌نمایی نکن.
خروجی را فقط به صورت یک شیء JSON فارسی/انگلیسی با این کلیدها بده:
{
  "first_name": "نام کوچک یا خالی",
  "full_name": "نام و نام خانوادگی کامل یا خالی",
  "role": "تخصص اصلی در یک عبارت کوتاه فارسی، مثل «برنامه‌نویس فول‌استک»",
  "years_experience": عدد صحیح سال سابقه (اگر در متن نبود 0),
  "stack": ["فهرست مهارت‌ها و تکنولوژی‌های واقعاً ذکرشده"],
  "highlights": ["۲ تا ۴ دستاورد یا نمونه‌کار واقعی، هر کدام یک جمله‌ی کوتاه"],
  "portfolio_url": "لینک گیت‌هاب یا سایت شخصی اگر در متن بود، وگرنه رشته خالی"
}
قواعد: حداکثر ۱۲ مورد در stack و ۴ مورد در highlights. هیچ متن اضافه‌ای بیرون JSON ننویس."""


async def analyse_text(
    text: str,
    *,
    kind: str,
    provider: LLMProvider | None = None,
    llm_config: Any | None = None,
    hints: dict[str, Any] | None = None,
) -> ImportedProfile:
    """Extract profile fields; falls back to heuristics when the LLM is unusable."""
    text = normalise_text(text)[:MAX_TEXT_CHARS]
    if len(text) < 30:
        raise ProfileImportError("متن کافی برای تحلیل پیدا نشد.")

    result = ImportedProfile(kind=kind, chars=len(text))
    if provider is not None:
        try:
            payload = await _llm_extract(text, provider, llm_config, kind)
        except Exception as exc:  # network, quota, bad JSON… — heuristics still work
            log.warning("LLM profile extraction failed: %s", exc)
            msg = str(exc)
            # Groq retired qwen/qwen3-32b on 2026-07-17 — guide to current models
            if "model_not_found" in msg.lower() or "does not exist" in msg.lower() or "مدل یافت نشد" in msg:
                result.warnings.append(f"تحلیل با هوش مصنوعی انجام نشد ({msg}) — این مدل منسوخ شده. با /llm مدل به‌روز مثل openai/gpt-oss-120b یا qwen/qwen3.6-27b را انتخاب کن؛ از روش جایگزین استفاده شد.")
            else:
                result.warnings.append(f"تحلیل با هوش مصنوعی انجام نشد ({msg})؛ از روش جایگزین استفاده شد.")
            payload = None
        if payload:
            result.used_llm = True
            result.fields = clean_fields(payload, hints=hints)
            if result.fields:
                return result

    result.fields = clean_fields(heuristic_extract(text), hints=hints)
    if not result.fields:
        result.warnings.append("اطلاعات قابل استخراجی پیدا نشد؛ با /profile دستی وارد کن.")
    return result


async def _llm_extract(
    text: str,
    provider: LLMProvider,
    llm_config: Any | None,
    kind: str,
) -> dict[str, Any] | None:
    from .proposals.llm_client import LLMClient

    client = LLMClient(
        base_url=provider.base_url,
        api_key=provider.api_key,
        model=provider.model,
        temperature=0,
        max_tokens=1200,
        timeout=float(getattr(llm_config, "timeout_seconds", 60.0) or 60.0),
        extra_headers=dict(provider.extra_headers or {}),
        max_retries=1,
    )
    origin = {
        "resume": "این متن یک رزومه است.",
        "github": "این متن خلاصه‌ی پروفایل و مخزن‌های گیت‌هاب است.",
        "portfolio": "این متن یک صفحه‌ی سایت شخصی/پورتفولیو است.",
    }.get(kind, "")
    try:
        response = await client.complete(SYSTEM_PROMPT, f"{origin}\n\n---\n{text}\n---")
    finally:
        await client.aclose()
    return parse_json_object(response.text)


def clean_fields(payload: dict[str, Any], *, hints: dict[str, Any] | None = None) -> dict[str, Any]:
    """Coerce an LLM/heuristic payload into exactly the fields we store."""
    hints = hints or {}
    fields: dict[str, Any] = {}

    def _text(key: str, limit: int = 120) -> str:
        value = payload.get(key)
        if isinstance(value, (list, tuple)):
            value = next((item for item in value if str(item).strip()), "")
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        return text[:limit]

    first_name = _text("first_name", 40)
    full_name = _text("full_name", 80)
    if not first_name and full_name:
        first_name = full_name.split(" ")[0]
    if first_name:
        fields["first_name"] = first_name
    if full_name:
        fields["full_name"] = full_name

    role = _text("role", 80)
    if role:
        fields["role"] = role

    try:
        years = int(str(payload.get("years_experience", 0)).translate(_DIGITS).strip() or 0)
    except (TypeError, ValueError):
        years = 0
    if 0 < years <= 60:
        fields["years_experience"] = years

    stack = _as_string_list(payload.get("stack"), limit=MAX_STACK, item_chars=40)
    if stack:
        fields["stack"] = stack

    highlights = _as_string_list(payload.get("highlights"), limit=MAX_HIGHLIGHTS, item_chars=180)
    if highlights:
        fields["highlights"] = highlights

    portfolio = _text("portfolio_url", 200) or str(hints.get("portfolio_url") or "").strip()
    if portfolio and re.match(r"^https?://", portfolio, re.I):
        fields["portfolio_url"] = portfolio
    elif hints.get("portfolio_url"):
        fields["portfolio_url"] = str(hints["portfolio_url"])

    return fields


def _as_string_list(value: Any, *, limit: int, item_chars: int) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[،,;•\n]", value)
    if not isinstance(value, (list, tuple, set)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = re.sub(r"\s+", " ", str(item or "")).strip(" -•\t")
        if len(text) < 2:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text[:item_chars])
        if len(out) >= limit:
            break
    return out


def heuristic_extract(text: str) -> dict[str, Any]:
    """Deterministic extraction — no network, no key, no surprises."""
    text = normalise_text(text)
    lowered = text.casefold()
    fields: dict[str, Any] = {}

    # ---- skills ---------------------------------------------------------
    stack: list[str] = []
    for tech in STACK_VOCABULARY:
        needle = tech.casefold()
        # word boundaries keep "SQL" out of "PostgreSQL" and "Java" out of "JavaScript"
        pattern = rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])"
        if re.search(pattern, lowered) and tech not in stack:
            stack.append(tech)
    if stack:
        fields["stack"] = stack[:MAX_STACK]

    # ---- years ----------------------------------------------------------
    years = 0
    for match in _YEARS_RE.finditer(text):
        try:
            value = int(match.group(1))
        except ValueError:
            continue
        if 0 < value <= 60:
            years = max(years, value)
    if years:
        fields["years_experience"] = years

    # ---- role -----------------------------------------------------------
    role = ""
    for needle, label in _ROLE_KEYWORDS:
        if needle in lowered:
            role = label
            break
    if not role:
        for needle, label in _PERSIAN_ROLE_WORDS:
            if needle in text:
                role = label
                break
    if role:
        fields["role"] = role

    # ---- name -----------------------------------------------------------
    for line in text.splitlines():
        candidate = line.strip()
        if not (3 <= len(candidate) <= 40):
            continue
        if re.search(r"[\d@:/\\]{1,}", candidate):
            continue
        if re.search(r"(رزومه|resume|curriculum|vitae|github|portfolio)", candidate, re.I):
            continue
        words = candidate.split()
        if 2 <= len(words) <= 4 and all(word[:1].isalpha() for word in words):
            fields["full_name"] = candidate
            fields["first_name"] = words[0]
            break

    # ---- portfolio ------------------------------------------------------
    github = _GITHUB_RE.search(text)
    if github:
        fields["portfolio_url"] = f"https://github.com/{github.group(1)}"
    else:
        url = _URL_RE.search(text)
        if url:
            fields["portfolio_url"] = url.group(0).rstrip(".,;")

    # ---- highlights -----------------------------------------------------
    bullets = [
        re.sub(r"^[-•*\d.\s)]+", "", line).strip()
        for line in text.splitlines()
        if re.match(r"^\s*[-•*]\s+", line) or line.strip().startswith("•")
    ]
    bullets = [item for item in bullets if 15 <= len(item) <= 200]
    if bullets:
        fields["highlights"] = bullets[:MAX_HIGHLIGHTS]

    return fields


# ---------------------------------------------------------------------------
# High-level imports (read the stored config, write the stored profile)
# ---------------------------------------------------------------------------
async def _provider_from_settings(store: SettingsStore) -> tuple[LLMProvider | None, Any]:
    from .config import Secrets, load_config, secrets_from_store

    try:
        cfg = load_config(store=store)
    except Exception as exc:  # a broken config must not block a profile import
        log.warning("Could not load config for profile import: %s", exc)
        return None, None
    secrets = secrets_from_store(Secrets(), store)
    chain = resolve_chain(cfg.llm, secrets.llm_keys, secrets.llm_api_key)
    return (chain[0] if chain else None), cfg.llm


async def _provider_for_user(store: SettingsStore, chat_id: str | int | None = None) -> tuple[LLMProvider | None, Any]:
    """Per-user provider — uses that user's own API key/model, never global.

    If chat_id is given and that user has a verified LLM config, it is used
    exclusively. Otherwise falls back to the legacy global provider so old
    single-user installs keep working.
    """
    if chat_id is not None:
        try:
            from .per_user import build_user_llm_provider

            provider = build_user_llm_provider(store, chat_id)
            if provider is not None:
                from .config import load_config

                cfg = load_config(store=store)
                return provider, cfg.llm
        except Exception as exc:
            log.debug("Per-user provider lookup failed: %s", exc)
    # fallback to global
    return await _provider_from_settings(store)


async def import_from_text(
    text: str,
    *,
    kind: str,
    store: SettingsStore,
    hints: dict[str, Any] | None = None,
    chat_id: str | int | None = None,
) -> ImportedProfile:
    provider, llm_config = await _provider_for_user(store, chat_id)
    return await analyse_text(text, kind=kind, provider=provider, llm_config=llm_config, hints=hints)


async def import_from_pdf(
    data: bytes,
    *,
    store: SettingsStore,
    filename: str = "resume.pdf",
    save_dir: str | Path | None = None,
    chat_id: str | int | None = None,
) -> ImportedProfile:
    text = pdf_text(data)
    result = await import_from_text(text, kind="resume", store=store, chat_id=chat_id)

    data_dir = Path(save_dir) if save_dir else Path("data") / "uploads"
    data_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    digest = hashlib.sha1(data).hexdigest()[:8]
    target = data_dir / f"resume-{stamp}-{digest}.pdf"
    target.write_bytes(data)
    if chat_id is not None:
        from .per_user import update_user

        update_user(store, chat_id, {"profile.resume_path": str(target), "profile.resume_filename": filename})
    else:
        store.update(
            {
                "profile.resume_path": str(target),
                "profile.resume_filename": filename,
            }
        )
    return result


async def import_from_url(url: str, *, store: SettingsStore, kind: str | None = None, chat_id: str | int | None = None) -> ImportedProfile:
    target = normalise_url(url)
    resolved_kind = kind or ("github" if _GITHUB_RE.match(target) else "portfolio")
    # Pass store/chat_id so fetch can use proxy if configured
    proxy = ""
    try:
        proxy = str(store.get("network.proxy") or store.get("llm.proxy") or "").strip()
    except Exception:
        pass
    text = await fetch_url_text(target, proxy=proxy, store=store, chat_id=chat_id)
    hints = {"portfolio_url": target}
    return await import_from_text(text, kind=resolved_kind, store=store, hints=hints, chat_id=chat_id)


PENDING_KEY = "profile.import_pending"


def pending_import(store: SettingsStore, chat_id: str | int | None = None) -> dict[str, Any] | None:
    """A resume/URL answer waiting to be analysed (set by the wizard)."""
    # Per-user first
    if chat_id is not None:
        from .per_user import get_user
        pending = get_user(store, chat_id, PENDING_KEY)
        if isinstance(pending, dict):
            value = str(pending.get("value") or "").strip()
            kind = str(pending.get("type") or "").strip()
            if value and kind in {"file", "url"}:
                return {"type": kind, "value": value}
    pending = store.get(PENDING_KEY)
    if not isinstance(pending, dict):
        return None
    value = str(pending.get("value") or "").strip()
    kind = str(pending.get("type") or "").strip()
    if not value or kind not in {"file", "url"}:
        return None
    return {"type": kind, "value": value}


def clear_pending_import(store: SettingsStore, chat_id: str | int | None = None) -> None:
    if chat_id is not None:
        from .per_user import delete_user
        delete_user(store, chat_id, PENDING_KEY)
    store.delete(PENDING_KEY)


def clear_import_error(store: SettingsStore, chat_id: str | int | None = None) -> None:
    """A successful import (or a fresh manual entry) forgets the last failure."""
    if chat_id is not None:
        from .per_user import delete_user
        delete_user(store, chat_id, "profile.import_error")
    store.delete("profile.import_error")


async def run_pending_import(
    store: SettingsStore,
    *,
    save_dir: str | Path | None = None,
    chat_id: str | int | None = None,
) -> ImportedProfile | None:
    """Analyse the queued resume/URL, store the fields, clear the queue."""
    pending = pending_import(store, chat_id=chat_id)
    if pending is None:
        return None

    if pending["type"] == "file":
        path = Path(pending["value"]).expanduser()
        if not path.is_file():
            raise ProfileImportError(f"فایل پیدا نشد: {path}")
        result = await import_from_pdf(path.read_bytes(), store=store, filename=path.name, save_dir=save_dir, chat_id=chat_id)
    else:
        result = await import_from_url(pending["value"], store=store, chat_id=chat_id)

    save_profile_fields(store, result.fields, source=result.kind, chat_id=chat_id)
    clear_pending_import(store, chat_id=chat_id)
    return result


def save_profile_fields(store: SettingsStore, fields: dict[str, Any], *, source: str, chat_id: str | int | None = None) -> dict[str, Any]:
    """Persist extracted fields (never clobbering a value the user typed later).

    When chat_id is given, fields are stored under users.{chat_id}.* so the
    extraction uses exactly that user's API key/model and never leaks to others.
    """
    written: dict[str, Any] = {}
    if chat_id is not None:
        from .per_user import update_user, set_user

        updates: dict[str, Any] = {}
        for key, value in (fields or {}).items():
            if key not in PROFILE_FIELDS or value in (None, "", [], 0):
                continue
            updates[f"profile.{key}"] = value
            written[key] = value
        updates["profile.source"] = source
        updates["profile.imported_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        updates["profile.import_error"] = None
        update_user(store, chat_id, updates)
        # also clear error in that namespace explicitly if present
        try:
            set_user(store, chat_id, "profile.import_error", None)
        except Exception:
            pass
    else:
        updates: dict[str, Any] = {}
        for key, value in (fields or {}).items():
            if key not in PROFILE_FIELDS or value in (None, "", [], 0):
                continue
            updates[f"profile.{key}"] = value
            written[key] = value
        updates["profile.source"] = source
        updates["profile.imported_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        updates["profile.import_error"] = None  # a good import clears the last failure
        store.update(updates)
    from .config import reset_secret_cache
    reset_secret_cache()
    return written


def summary_text(fields: dict[str, Any], *, title: str = "پروفایل استخراج‌شده") -> str:
    """Human-readable, Telegram-friendly recap of what was extracted."""
    from html import escape

    lines = [f"<b>{title}</b>", ""]
    labels = {
        "first_name": "نام",
        "full_name": "نام کامل",
        "role": "تخصص",
        "years_experience": "سابقه (سال)",
        "stack": "مهارت‌ها",
        "highlights": "نمونه‌کارها",
        "portfolio_url": "لینک",
    }
    for key in PROFILE_FIELDS:
        if key not in fields:
            continue
        value = fields[key]
        if isinstance(value, list):
            value = "، ".join(str(item) for item in value)
        lines.append(f"• <b>{labels.get(key, key)}:</b> {escape(str(value))}")
    if len(lines) == 2:
        lines.append("(چیزی پیدا نشد)")
    return "\n".join(lines)


__all__ = [
    "clear_import_error",
    "ImportedProfile",
    "MAX_TEXT_CHARS",
    "PROFILE_FIELDS",
    "MissingDependency",
    "ProfileImportError",
    "STACK_VOCABULARY",
    "analyse_text",
    "clean_fields",
    "fetch_url_text",
    "heuristic_extract",
    "html_text",
    "import_from_pdf",
    "import_from_text",
    "import_from_url",
    "normalise_text",
    "normalise_url",
    "parse_json_object",
    "pdf_text",
    "pending_import",
    "clear_pending_import",
    "run_pending_import",
    "save_profile_fields",
    "summary_text",
]
