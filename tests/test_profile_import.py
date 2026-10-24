"""Resume / portfolio / GitHub → profile fields.

The whole point: the user sends *one* thing, the bot fills the profile. These
tests cover the three input paths, the LLM JSON parsing, the heuristic fallback
and the queue that connects the (sync) wizard to the (async) analysis.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from pdf_fixtures import make_pdf

from bidpilot import profile_import as pi
from bidpilot.settings_store import SettingsStore


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
@pytest.fixture()
def store(tmp_path) -> SettingsStore:
    return SettingsStore(tmp_path / "data" / "settings.json")


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def test_pdf_text_reads_a_real_pdf():
    data = make_pdf(
        [
            "Ali Eghbali",
            "Senior Flutter and ASP.NET Developer",
            "5 years of experience",
            "Skills: Flutter, C#, Unity, FastAPI",
            "- Built a shopping app for Avan",
            "https://github.com/kaspian021",
        ]
    )
    text = pi.pdf_text(data)
    assert "Ali Eghbali" in text
    assert "5 years" in text

    fields = pi.heuristic_extract(text)
    assert fields["full_name"] == "Ali Eghbali"
    assert fields["first_name"] == "Ali"
    assert fields["years_experience"] == 5
    assert "Flutter" in fields["stack"]
    assert fields["portfolio_url"] == "https://github.com/kaspian021"


def test_scanned_or_empty_pdf_is_rejected_with_a_helpful_message():
    with pytest.raises(pi.ProfileImportError) as exc:
        pi.pdf_text(make_pdf([""]))
    assert "اسکن" in str(exc.value) or "متنی" in str(exc.value)

    with pytest.raises(pi.ProfileImportError):
        pi.pdf_text(b"")


# ---------------------------------------------------------------------------
# heuristic extractor
# ---------------------------------------------------------------------------
def test_heuristic_extract_handles_persian_resume_text():
    text = """
    علی اقبالی
    برنامه‌نویس موبایل و وب
    ۳ سال سابقه کار
    مهارت‌ها: Flutter، Asp.net، Unity، FastAPI
    - پیاده‌سازی سایت loodoi از صفر تا انتشار
    - برنامه نویس نرم افزار Avan با تیم پیشروآتیه
    """
    fields = pi.heuristic_extract(text)
    assert fields["full_name"] == "علی اقبالی"
    assert fields["years_experience"] == 3  # Persian digit normalised
    assert any("Flutter" in item for item in fields["stack"])
    assert fields["highlights"]


def test_heuristic_does_not_confuse_sql_with_postgresql():
    fields = pi.heuristic_extract("Databases: PostgreSQL, MySQL, Redis")
    assert "SQL" not in fields.get("stack", [])
    assert "PostgreSQL" in fields["stack"]


def test_clean_fields_rejects_invented_values():
    cleaned = pi.clean_fields(
        {
            "first_name": "",
            "full_name": "Sara Ahmadi",
            "role": "  طراح   UI/UX ",
            "years_experience": "۹",  # Persian digits from an LLM
            "stack": ["Figma", "Figma", "", "UX"],
            "highlights": ["طراحی ۲۰ داشبورد"],
            "portfolio_url": "not-a-url",
        }
    )
    assert cleaned["full_name"] == "Sara Ahmadi"
    assert cleaned["first_name"] == "Sara"
    assert cleaned["role"] == "طراح UI/UX"
    assert cleaned["years_experience"] == 9
    assert cleaned["stack"] == ["Figma", "UX"]
    assert "portfolio_url" not in cleaned  # invalid URL is dropped, never invented


# ---------------------------------------------------------------------------
# LLM path
# ---------------------------------------------------------------------------
def test_parse_json_object_survives_markdown_fences_and_chatter():
    fenced = '```json\n{"role": "برنامه‌نویس"}\n```'
    assert pi.parse_json_object(fenced) == {"role": "برنامه‌نویس"}
    chatty = 'حتماً. خروجی: {"role": "طراح"} — امیدوارم مفید باشد.'
    assert pi.parse_json_object(chatty) == {"role": "طراح"}
    assert pi.parse_json_object("هیچ JSON ندارم") is None


@respx.mock
async def test_analyse_text_uses_llm_json(store):
    from bidpilot.llm_providers import LLMProvider

    payload = {
        "first_name": "علی",
        "full_name": "علی اقبالی",
        "role": "برنامه‌نویس فول‌استک",
        "years_experience": 3,
        "stack": ["Flutter", "ASP.NET", "Unity"],
        "highlights": ["اپلیکیشن اوان", "سایت loodoi"],
        "portfolio_url": "",
    }
    route = respx.post("https://api.example.test/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]},
        )
    )
    provider = LLMProvider(id="test", base_url="https://api.example.test", model="m", api_key="k")
    result = await pi.analyse_text(
        "علی اقبالی، ۳ سال تجربه، Flutter و ASP.NET", kind="resume", provider=provider
    )
    assert result.used_llm is True
    assert route.called
    assert result.fields["role"] == "برنامه‌نویس فول‌استک"
    assert result.fields["years_experience"] == 3


@respx.mock
async def test_analyse_text_falls_back_to_heuristics_when_the_llm_fails(store):
    from bidpilot.llm_providers import LLMProvider

    respx.post("https://api.example.test/chat/completions").mock(
        return_value=httpx.Response(401, json={"error": "bad key"})
    )
    provider = LLMProvider(id="test", base_url="https://api.example.test", model="m", api_key="bad")
    result = await pi.analyse_text(
        "Sara Ahmadi\nUI/UX Designer\n7 years of experience\nFigma, React", kind="resume", provider=provider
    )
    assert result.used_llm is False
    assert result.fields["full_name"] == "Sara Ahmadi"
    assert result.warnings


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------
@respx.mock
async def test_fetch_url_text_reads_a_portfolio_page():
    html = """
    <html><head><title>Ali</title><style>.x{}</style>
    <script>var secret = 1;</script></head>
    <body><h1>Ali Eghbali</h1><p>Flutter developer, 5 years</p>
    <ul><li>Built a shopping app</li></ul></body></html>
    """
    respx.get("https://portfolio.example/me").mock(return_value=httpx.Response(200, text=html))
    text = await pi.fetch_url_text("portfolio.example/me")
    assert "Ali Eghbali" in text
    assert "secret" not in text  # scripts are stripped
    assert "Flutter developer" in text


@respx.mock
async def test_fetch_url_text_uses_the_github_api():
    respx.get("https://api.github.com/users/kaspian021").mock(
        return_value=httpx.Response(
            200,
            json={
                "login": "kaspian021",
                "name": "Ali Eghbali",
                "bio": "Flutter & ASP.NET developer",
                "public_repos": 12,
            },
        )
    )
    respx.get("https://api.github.com/users/kaspian021/repos").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"name": "loodoi", "language": "C#", "description": "e-commerce", "fork": False},
                {"name": "fork", "language": "Dart", "description": "not mine", "fork": True},
            ],
        )
    )
    text = await pi.fetch_url_text("https://github.com/kaspian021")
    assert "bio: Flutter & ASP.NET developer" in text
    assert "loodoi" in text
    assert "not mine" not in text  # forks are ignored


@respx.mock
async def test_fetch_url_text_reports_http_errors_in_persian():
    respx.get("https://portfolio.example/private").mock(return_value=httpx.Response(403))
    with pytest.raises(pi.ProfileImportError) as exc:
        await pi.fetch_url_text("https://portfolio.example/private")
    assert "403" in str(exc.value)


# ---------------------------------------------------------------------------
# The queue between wizard and analysis
# ---------------------------------------------------------------------------
async def test_run_pending_import_reads_the_resume_and_clears_the_queue(store):
    resume = make_pdf(
        [
            "Ali Eghbali",
            "Senior Flutter Developer",
            "5 years of experience",
            "Flutter, C#, Unity",
            "- Built the Avan app",
        ]
    )
    path = store.path.parent / "resume.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(resume)
    store.update(
        {
            "profile.resume_path": str(path),
            "profile.import_pending": {"type": "file", "value": str(path)},
        }
    )

    result = await pi.run_pending_import(store, save_dir=store.path.parent / "uploads")

    assert result is not None
    assert result.kind == "resume"
    assert store.get("profile.full_name") == "Ali Eghbali"
    assert store.get("profile.years_experience") == 5
    assert "Flutter" in store.get_list("profile.stack")
    assert store.get("profile.source") == "resume"
    assert store.get("profile.imported_at")
    assert pi.pending_import(store) is None  # queue cleared: never runs twice
    assert Path(store.get("profile.resume_path")).exists()


async def test_run_pending_import_without_a_queue_is_a_no_op(store):
    assert await pi.run_pending_import(store) is None


async def test_pending_import_covers_urls(store, monkeypatch):
    async def fake_url(url, *, store, kind=None):  # noqa: ANN001
        return pi.ImportedProfile(
            kind="github",
            fields={"full_name": "Ali Eghbali", "stack": ["C#"]},
            used_llm=True,
        )

    monkeypatch.setattr(pi, "import_from_url", fake_url)
    store.set("profile.import_pending", {"type": "url", "value": "https://github.com/kaspian021"})

    result = await pi.run_pending_import(store)

    assert result is not None and result.used_llm
    assert store.get("profile.full_name") == "Ali Eghbali"
    assert store.get("profile.source") == "github"


def test_summary_text_escapes_html_for_telegram():
    text = pi.summary_text({"full_name": "<script>x</script>", "stack": ["C#", "Flutter"]})
    assert "&lt;script&gt;" in text
    assert "Flutter" in text


# ---------------------------------------------------------------------------
# A missing pypdf must be fixable, not fatal
# ---------------------------------------------------------------------------
def test_missing_pypdf_explains_how_to_install_it_for_this_python(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "pypdf", None)  # ``from pypdf import …`` now fails

    with pytest.raises(pi.MissingDependency) as exc:
        pi.pdf_text(b"%PDF-1.4 something")

    message = str(exc.value)
    assert exc.value.module == "pypdf"
    assert sys.executable in message
    assert "-m pip install" in message


async def test_a_failed_import_keeps_the_queue_for_a_retry(store):
    path = store.path.parent / "broken.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4 not really a pdf")
    store.update(
        {
            "profile.resume_path": str(path),
            "profile.import_pending": {"type": "file", "value": str(path)},
        }
    )

    with pytest.raises(pi.ProfileImportError):
        await pi.run_pending_import(store)

    # the user can fix the cause (install pypdf, resend a text PDF) and retry
    assert pi.pending_import(store) == {"type": "file", "value": str(path)}
    assert store.get("profile.source") is None


async def test_a_successful_import_clears_an_old_error(store, monkeypatch):
    store.set("profile.import_error", "pypdf نصب نیست")
    store.set("profile.import_pending", {"type": "url", "value": "https://github.com/x"})

    async def fake_url(url, *, store, kind=None):  # noqa: ANN001
        return pi.ImportedProfile(kind="github", fields={"full_name": "Ali"})

    monkeypatch.setattr(pi, "import_from_url", fake_url)
    await pi.run_pending_import(store)

    assert store.get("profile.import_error") is None
    assert store.get("profile.imported_at")
