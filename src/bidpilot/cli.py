"""Command line interface (Typer + Rich)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .app import Application, build_application, configure_logging
from .config import AppConfig, get_secrets, load_config, reset_secret_cache
from .notifications.telegram import TelegramNotifier
from .pricing.strategy import PricingConfig
from .proposals.generator import ProposalGenerator
from .settings_store import SettingsStore, get_store
from .sources import available_sources
from .text import to_persian_digits

app = typer.Typer(
    add_completion=False, help="BidPilot — automated, human-quality bids for freelance marketplaces."
)
console = Console()

CONFIG_OPTION = typer.Option("config/config.yaml", "--config", "-c", help="مسیر فایل تنظیمات")


def _load(config_path: str) -> AppConfig:
    path = Path(config_path)
    if not path.exists():
        console.print(f"[yellow]فایل تنظیمات پیدا نشد: {path} — از پیش‌فرض‌ها استفاده می‌شود.[/yellow]")
    cfg = load_config(path)
    configure_logging(cfg.log_level)
    return cfg


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------- basics
@app.command()
def version() -> None:
    """نمایش نسخه."""
    console.print(f"BidPilot [bold cyan]{__version__}[/bold cyan]")


# ----------------------------------------------------------------- setup / bot
@app.command()
def setup(
    config: str = CONFIG_OPTION,
    stage: str | None = typer.Option(
        None,
        "--stage",
        help="فقط یک بخش: telegram | llm | market | profile | style | mode",
    ),
    status_only: bool = typer.Option(False, "--status", help="فقط نمایش وضعیت فعلی"),
) -> None:
    """راه‌اندازی گام‌به‌گام — بدون نیاز به .env و بدون دست‌زدن به فایل‌ها.

    همه‌ی پاسخ‌ها (توکن تلگرام، کلید هوش مصنوعی رایگان، حساب سایت، پروفایل، لحن)
    امن در data/settings.json ذخیره می‌شوند و بعداً از داخل تلگرام هم قابل تغییرند.
    """
    from .onboarding import STAGE_TITLES, run_terminal_wizard

    configure_logging("WARNING")
    store = get_store()
    if status_only:
        _print_settings_status(store)
        return
    if stage and stage not in STAGE_TITLES:
        raise typer.BadParameter(f"بخش نامعتبر است. یکی از: {', '.join(STAGE_TITLES)}")
    run_terminal_wizard(store, console, stages=[stage] if stage else None)
    console.print(f"\n[dim]محل ذخیره: {store.path}[/dim]")


@app.command()
def settings(
    action: str = typer.Argument("show", help="show | set | unset | path"),
    key: str | None = typer.Argument(None, help="کلید نقطه‌ای، مثل proposal.tone"),
    value: str | None = typer.Option(None, "--value", "-v", help="مقدار برای set"),
    reveal: bool = typer.Option(False, "--reveal", help="نمایش کامل مقادیر حساس"),
    as_json: bool = typer.Option(False, "--json", help="تفسیر مقدار به‌عنوان JSON"),
) -> None:
    """مدیریت تنظیمات ذخیره‌شده (بدون .env)."""
    import json as _json

    store = get_store()
    action = action.strip().lower()
    if action == "path":
        console.print(str(store.path))
        return
    if action == "show":
        _print_settings_status(store, reveal=reveal)
        return
    if action in {"set", "unset"} and not key:
        raise typer.BadParameter('کلید را بده: bidpilot settings set proposal.tone -v "رسمی"')
    if action == "unset":
        removed = store.delete(key)  # type: ignore[arg-type]
        reset_secret_cache()
        console.print("[green]حذف شد.[/green]" if removed else "[yellow]کلیدی با این نام نبود.[/yellow]")
        return
    if action == "set":
        if value is None:
            raise typer.BadParameter("مقدار را با --value بده.")
        if as_json:
            try:
                parsed = _json.loads(value)
            except ValueError as exc:
                raise typer.BadParameter(f"مقدار JSON معتبر نیست: {exc}") from exc
        else:
            parsed = value
        store.set(key, parsed)  # type: ignore[arg-type]
        reset_secret_cache()
        console.print(f"[green]ذخیره شد:[/green] {key}")
        return
    raise typer.BadParameter("دستور نامعتبر. یکی از: show | set | unset | path")


def _print_settings_status(store: SettingsStore, *, reveal: bool = False) -> None:
    from .onboarding import readiness

    state = readiness(store)
    table = Table(title="وضعیت راه‌اندازی")
    table.add_column("بخش")
    table.add_column("وضعیت")
    table.add_row("تلگرام", "✅ متصل" if state["telegram"] else "❌ توکن/chat_id ثبت نشده")
    table.add_row(
        "هوش مصنوعی",
        f"{'✅' if state['llm'] else '❌'} {state['llm_title']} — {state['llm_model']}",
    )
    table.add_row(
        "سایت فریلنسری",
        f"{'✅' if state['credentials'] else '❌'} {state['marketplace']}",
    )
    table.add_row("لحن", state["tone"] or "—")
    table.add_row("دستورهای سبک", "ثبت شده" if state["style"] else "—")
    table.add_row("حالت ارسال", "واقعی" if state["mode"] == "live" else "آزمایشی")
    table.add_row("راه‌اندازی", "کامل" if state["setup_completed"] else "ناتمام")
    console.print(table)

    rows = store.masked_items()
    if rows:
        details = Table(title=f"تنظیمات ذخیره‌شده — {store.path}")
        details.add_column("کلید")
        details.add_column("مقدار")
        for name, shown in rows:
            if reveal:
                raw = store.get(name)
                shown = str(raw)
            details.add_row(escape(name), escape(shown))
        console.print(details)


@app.command()
def bot(
    config: str = CONFIG_OPTION,
    with_runner: bool = typer.Option(False, "--run", help="همراه با رصد و ارسال خودکار در همین فرآیند"),
    interval: int | None = typer.Option(None, "--interval", help="فاصله بین دورها (ثانیه)"),
    source: list[str] | None = typer.Option(None, "--source", "-s", help="فقط این منابع"),
) -> None:
    """کنترل کامل ربات از تلگرام: راه‌اندازی، لحن، وضعیت و پیش‌نمایش.

    اگر توکن هنوز ذخیره نشده باشد، اول «bidpilot setup» را اجرا کن.
    """

    async def main() -> None:
        from .bot import BotAPIError, build_bot

        store = get_store()
        cfg = _load(config)
        token = get_secrets().telegram_bot_token or store.get("telegram.bot_token")
        if not token:
            console.print(
                "[red]توکن ربات تلگرام تنظیم نشده است.[/red]\n"
                "  [bold]bidpilot setup[/bold]  → سؤال‌ها را جواب بده\n"
                "  یا: [bold]bidpilot settings set telegram.bot_token -v <TOKEN>[/bold]"
            )
            raise typer.Exit(code=1)

        service = build_bot(str(token), store=store, config_path=config)
        application = None
        scheduler = None
        tasks = [asyncio.create_task(service.run_forever(), name="telegram-bot")]
        if with_runner:
            application = build_application(cfg)
            await application.setup()
            from .scheduler import Scheduler

            def make_app():
                return build_application(load_config(config))

            scheduler = Scheduler(application, interval=interval, sources=source, app_factory=make_app)
            tasks.append(asyncio.create_task(scheduler.run_forever(), name="scheduler"))
            console.print("[green]ربات و رصد خودکار هر دو فعال شدند.[/green]")
        else:
            console.print(
                "[green]ربات فعال شد.[/green] در تلگرام /start را بزن. "
                "برای رصد خودکار: [bold]bidpilot run[/bold] یا [bold]bidpilot bot --run[/bold]"
            )

        try:
            await asyncio.gather(*tasks)
        except BotAPIError as exc:
            console.print(
                f"[red]اتصال به تلگرام برقرار نشد:[/red] {exc}\n"
                "توکن را بررسی کن: [bold]bidpilot settings set telegram.bot_token -v <TOKEN>[/bold]"
            )
            raise typer.Exit(code=1) from exc
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            if scheduler is not None:
                scheduler.request_stop()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await service.api.aclose()
            if application is not None:
                await application.close()

    _run(main())


@app.command("sources")
def sources_cmd(config: str = CONFIG_OPTION) -> None:
    """لیست منابع (مارکت‌پلیس‌های) ثبت‌شده و فعال."""
    cfg = _load(config)
    table = Table(title="Sources")
    table.add_column("type")
    table.add_column("registered")
    table.add_column("enabled in config")
    registered = set(available_sources())
    configured = {s.type: s.enabled for s in cfg.sources}
    for name in sorted(registered | set(configured)):
        table.add_row(name, "✅" if name in registered else "—", "✅" if configured.get(name) else "—")
    console.print(table)


@app.command()
def initdb(config: str = CONFIG_OPTION) -> None:
    """ساخت جدول‌های دیتابیس."""

    async def main() -> None:
        cfg = _load(config)
        application = build_application(cfg)
        await application.db.init()
        await application.db.dispose()
        console.print(f"[green]دیتابیس آماده شد:[/green] {cfg.database_url}")

    _run(main())


@app.command()
def doctor(config: str = CONFIG_OPTION) -> None:
    """بررسی سلامت تنظیمات، کلیدها و دسترسی به منابع."""

    async def main() -> None:
        cfg = _load(config)
        application = build_application(cfg)
        await application.db.init()

        table = Table(title="Doctor")
        table.add_column("بررسی")
        table.add_column("وضعیت")
        table.add_column("توضیح")

        def row(name: str, ok: bool, detail: str = "") -> None:
            table.add_row(name, "[green]OK[/green]" if ok else "[red]FAIL[/red]", detail)

        profile = cfg.profile
        profile_issues = profile.problems()
        profile_ok = not profile_issues
        row(
            "پروفایل",
            profile_ok,
            f"{profile.full_name} | {profile.stack_short}" if profile_ok else "؛ ".join(profile_issues),
        )

        tg_ok = bool(application.secrets.telegram_bot_token and cfg.notifications.chat_ids)
        row("تلگرام", tg_ok, f"{len(cfg.notifications.chat_ids)} chat id")

        provider_key = application.secrets.llm_keys.get(cfg.llm.provider) or (application.secrets.llm_api_key)
        llm_ok = bool(cfg.llm.enabled and provider_key)
        row(
            "مدل زبانی",
            llm_ok,
            f"{cfg.llm.provider_title} — {cfg.llm.model}"
            if llm_ok
            else "کلید رایگان ثبت نشده → متن با قالب نوشته می‌شود (bidpilot setup)",
        )

        store = get_store()
        creds_ok = bool(
            application.secrets.parscoders_cookie
            or (application.secrets.parscoders_username and application.secrets.parscoders_password)
            or (store.get("marketplace.name") == "mock")
        )
        row(
            "حساب سایت",
            creds_ok,
            f"{store.get('marketplace.name') or 'parscoders'} — "
            + ("اعتبارنامه ثبت شده" if creds_ok else "در bidpilot setup وارد کن"),
        )
        row(
            "تنظیمات ذخیره‌شده",
            store.exists,
            str(store.path) if store.exists else "هنوز چیزی ذخیره نشده (bidpilot setup)",
        )

        row("حالت ارسال", True, "LIVE" if cfg.runtime.live else "DRY-RUN (پیشنهاد ارسال نمی‌شود)")

        for name, (ok, detail) in (await application.healthcheck()).items():
            row(f"منبع: {name}", ok, detail)

        await application.close()
        console.print(table)

    _run(main())


@app.command()
def stats(config: str = CONFIG_OPTION) -> None:
    """آمار پروژه‌ها و پیشنهادهای ثبت‌شده."""

    async def main() -> None:
        cfg = _load(config)
        application = build_application(cfg)
        await application.db.init()
        session = application.db.session()
        from .repositories import Repository

        repo = Repository(session)
        data = await repo.stats()
        table = Table(title="Stats")
        table.add_column("کلید")
        table.add_column("مقدار")
        for key, value in sorted(data.items()):
            table.add_row(key, str(value))
        console.print(table)

        rows = await repo.recent_bids(limit=10)
        if rows:
            bids = Table(title="آخرین پیشنهادها")
            for column in ("id", "source", "status", "amount", "generated_by"):
                bids.add_column(column)
            for r in rows:
                bids.add_row(
                    str(r.id),
                    r.source,
                    r.status,
                    f"{r.amount:,}" if r.amount else "—",
                    r.proposal_generated_by,
                )
            console.print(bids)
        await session.close()
        await application.close()

    _run(main())


# ----------------------------------------------------------------------- runs
@app.command()
def once(
    config: str = CONFIG_OPTION,
    source: list[str] | None = typer.Option(None, "--source", "-s", help="فقط این منابع"),
    live: bool = typer.Option(False, "--live", help="ارسال واقعی پیشنهاد (تنظیم config را override می‌کند)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="فقط شبیه‌سازی، بدون ارسال"),
    limit: int | None = typer.Option(None, "--limit", help="حداکثر تعداد پروژه در این دور"),
    template_only: bool = typer.Option(False, "--template-only", help="بدون LLM، فقط قالب"),
) -> None:
    """یک دور کامل: دریافت پروژه‌ها، قیمت‌گذاری، تولید متن و ارسال."""

    async def main() -> None:
        cfg = _load(config)
        if live and dry_run:
            raise typer.BadParameter("--live و --dry-run همزمان معنا ندارند.")
        application = build_application(cfg)
        await application.setup()
        if template_only:
            cfg.llm.enabled = False
        reports = await application.cycle(
            source, live=True if live else (False if dry_run else None), max_projects=limit
        )
        table = Table(title="Run report")
        for column in ("source", "fetched", "new", "duplicates", "submitted", "dry_run", "skipped", "failed"):
            table.add_column(column)
        for r in reports:
            table.add_row(
                r.source,
                str(r.fetched),
                str(r.new),
                str(r.duplicates),
                str(r.submitted),
                str(r.dry_run),
                str(r.skipped),
                str(r.failed),
            )
        console.print(table)
        await application.close()

    _run(main())


@app.command()
def run(
    config: str = CONFIG_OPTION,
    source: list[str] | None = typer.Option(None, "--source", "-s"),
    interval: int | None = typer.Option(None, "--interval", help="فاصله بین دورها (ثانیه)"),
    live: bool = typer.Option(False, "--live", help="ارسال واقعی"),
) -> None:
    """اجرای دائمی (daemon) با فاصله‌ی تصادفی."""

    async def main() -> None:
        cfg = _load(config)
        if live:
            cfg.runtime.submit_mode = "live"
        application = build_application(cfg)
        await application.setup()
        from .scheduler import Scheduler

        def make_app():
            fresh = load_config(config)
            if live:
                fresh.runtime.submit_mode = "live"
            return build_application(fresh)

        scheduler = Scheduler(application, interval=interval, sources=source, app_factory=make_app)
        try:
            await scheduler.run_forever()
        finally:
            await application.close()

    _run(main())


@app.command()
def preview(
    config: str = CONFIG_OPTION,
    source: str | None = typer.Option(None, "--source", "-s"),
    limit: int = typer.Option(3, "--limit"),
    template_only: bool = typer.Option(False, "--template-only", help="بدون فراخوانی LLM"),
) -> None:
    """نمایش پیشنهادهای تولیدی بدون ارسال و بدون ذخیره در دیتابیس."""

    async def main() -> None:
        cfg = _load(config)
        application = build_application(cfg)
        await application.setup()
        name = source or next(iter(application.sources), None)
        if not name:
            console.print("[red]هیچ منبعی فعال نیست.[/red]")
            return
        src = application.sources.get(name)
        if src is None:  # allow previewing a source that is disabled in config
            from .config import get_secrets
            from .sources import create_source

            entry = cfg.source(name)
            src = create_source(name, (entry.settings if entry else {}), cfg, get_secrets())
            application.sources[name] = src
        projects = await src.fetch_projects()
        pricing = PricingConfig(**cfg.pricing).build()
        generator: ProposalGenerator = application.generator  # type: ignore[assignment]
        console.print(f"[bold]{len(projects)}[/bold] پروژه از منبع [cyan]{name}[/cyan] دریافت شد.\n")
        for project in projects[:limit]:
            if not application.filter_preview().evaluate(project):
                console.print(f"[yellow]رد شد (فیلتر):[/yellow] {project.title}")
                continue
            pr = pricing.price(project, project.budget)
            if pr is None:
                continue
            proposal = await generator.generate(
                project, pr.amount, int(cfg.pricing.get("delivery_days", 14)), force_template=template_only
            )
            console.print(
                Panel(
                    proposal.text,
                    title=f"{project.title} | {to_persian_digits(f'{pr.amount:,}')} تومان | {proposal.generated_by}",
                    subtitle=project.url,
                    border_style="cyan",
                )
            )
        await application.close()

    _run(main())


@app.command()
def demo(
    config: str = CONFIG_OPTION,
    live: bool = typer.Option(False, "--live", help="ارسال به منبع آزمایشی (mock)"),
) -> None:
    """اجرای کامل روی منبع آزمایشی، بدون شبکه و بدون حساب واقعی."""

    async def main() -> None:
        cfg = _load(config)
        from .config import SourceConfig

        cfg.sources = [SourceConfig(type="mock", settings={})]
        cfg.llm.enabled = False  # deterministic demo
        cfg.database_url = "sqlite+aiosqlite:///data/demo.sqlite3"
        cfg.notifications.enabled = True
        Path("data").mkdir(exist_ok=True)
        demo_db = Path("data/demo.sqlite3")
        if demo_db.exists():
            demo_db.unlink()

        application = Application.build(cfg)
        await application.setup()
        reports = await application.cycle(live=live)
        for r in reports:
            console.print(f"[bold]{r.summary()}[/bold]")
        await application.close()

    _run(main())


@app.command()
def inspect(
    url: str | None = typer.Option(None, "--url", "-u", help="آدرس صفحه برای بررسی"),
    file: str | None = typer.Option(None, "--file", "-f", help="فایل HTML ذخیره‌شده از مرورگر"),
    config: str = CONFIG_OPTION,
    save: bool = typer.Option(True, "--save/--no-save", help="ذخیره‌ی HTML در data/debug"),
) -> None:
    """دیباگ سلکتورها: کدام سلکتور چند مورد پیدا می‌کند؟ (وقتی ظاهر سایت عوض می‌شود)

    مثال:
      bidpilot inspect -u https://parscoders.com/project
      bidpilot inspect -f ~/Downloads/project.html
    """

    async def main() -> None:
        import httpx
        from bs4 import BeautifulSoup

        cfg = _load(config)
        from .config import get_secrets
        from .sources.parscoders.adapter import load_default_settings

        settings = cfg.source("parscoders").settings if cfg.source("parscoders") else {}
        defaults = load_default_settings()
        for key, value in (settings or {}).items():
            if isinstance(value, dict) and isinstance(defaults.get(key), dict):
                defaults[key] = {**defaults[key], **value}
            else:
                defaults[key] = value
        list_cfg = defaults.get("list", {})

        if file:
            html = Path(file).expanduser().read_text(encoding="utf-8", errors="replace")
            shown_url = file
        else:
            target = (
                url
                or f"{defaults.get('base_url', 'https://parscoders.com')}{list_cfg.get('path', '/project')}"
            )
            shown_url = target
            secrets = get_secrets()
            from .sources.parscoders.adapter import parse_cookie_header

            cookies = parse_cookie_header(secrets.parscoders_cookie)
            async with httpx.AsyncClient(
                headers={"User-Agent": cfg.runtime.user_agent, "Accept-Language": "fa-IR,fa;q=0.9"},
                cookies=cookies,
                timeout=cfg.runtime.request_timeout_seconds,
                follow_redirects=True,
            ) as client:
                response = await client.get(target)
            console.print(
                f"HTTP [bold]{response.status_code}[/bold] — {len(response.text):,} bytes — {response.url}"
            )
            html = response.text

        if save:
            debug_dir = Path(cfg.data_dir) / "debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            out = debug_dir / f"inspect-{int(__import__('time').time())}.html"
            out.write_text(html, encoding="utf-8")
            console.print(f"HTML ذخیره شد: [cyan]{out}[/cyan]")

        soup = BeautifulSoup(html, "lxml")
        table = Table(title=f"Card selectors — {shown_url}")
        table.add_column("selector")
        table.add_column("matches", justify="right")
        for selector in list_cfg.get("card_selectors", []):
            try:
                count = len(soup.select(selector))
            except Exception as exc:  # invalid selector
                console.print(f"[red]سلکتور نامعتبر:[/red] {selector} ({exc})")
                continue
            table.add_row(escape(selector), f"[green]{count}[/green]" if count else "[dim]0[/dim]")
        console.print(table)

        id_regex = list_cfg.get("id_regex", r"/projects?/(\d+)")
        import re as _re

        links = [a["href"] for a in soup.find_all("a", href=True) if _re.search(id_regex, a["href"])]
        console.print(f"لینک‌های پروژه‌مانند: [bold]{len(links)}[/bold]")
        for link in links[:5]:
            console.print(f"  • {link}")

        for label, field in (("بودجه", "budget"), ("عنوان", "title")):
            sel = (list_cfg.get("fields") or {}).get(field) or []
            console.print(f"\n[f]سلکتورهای {label}:[/f]")
            for selector in sel:
                try:
                    node = soup.select_one(selector)
                except Exception:
                    node = None
                text = node.get_text(" ", strip=True)[:80] if node else "—"
                console.print(f"  {'✅' if node else '❌'} {escape(selector)}  →  {escape(text)}")

        forms = soup.find_all("form")
        console.print(f"\nتعداد فرم‌ها در صفحه: [bold]{len(forms)}[/bold]")
        for i, form in enumerate(forms[:5]):
            fields = [inp.get("name") for inp in form.select("input,textarea,select") if inp.get("name")]
            console.print(f"  form#{i} action={form.get('action')} fields={fields}")

        console.print(
            "\n[dim]اگر همه‌ی سلکتورها ۰ بودند: سلکتورهای درست را از DevTools بردار و در "
            "src/bidpilot/sources/parscoders/config.yaml جایگزین کن.[/dim]"
        )

    _run(main())


# ------------------------------------------------------------------- telegram
@app.command("go-live")
def go_live(
    config: str = CONFIG_OPTION,
    off: bool = typer.Option(False, "--off", help="برگرداندن به حالت آزمایشی"),
    force: bool = typer.Option(False, "--force", help="نادیده گرفتن ایرادها (توصیه نمی‌شود)"),
) -> None:
    """آماده‌سازی و فعال‌سازی ارسال واقعی (با چک‌لیست ایمنی).

    این دستور فایل gitignored «config/config.local.yaml» و مقدار runtime.submit_mode را در
    data/settings.json می‌نویسد؛ تنظیمات اصلی (config.yaml) دست نمی‌خورد.
    """
    import yaml as _yaml

    from .config import get_secrets

    cfg = _load(config)
    local_path = Path(config).with_name("config.local.yaml")
    local_raw = {}
    if local_path.exists():
        local_raw = _yaml.safe_load(local_path.read_text(encoding="utf-8")) or {}

    store = get_store()
    if off:
        runtime = local_raw.setdefault("runtime", {}) or {}
        runtime["submit_mode"] = "dry_run"
        local_raw["runtime"] = runtime
        local_path.write_text(
            _yaml.safe_dump(local_raw, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        store.set("runtime.submit_mode", "dry_run")
        console.print(f"[yellow]حالت آزمایشی فعال شد.[/yellow] ({local_path})")
        return

    secrets = get_secrets()
    checks: list[tuple[str, bool, str, bool]] = []  # (label, ok, detail, blocking)

    profile_issues = cfg.profile.problems()
    checks.append(
        (
            "پروفایل",
            not profile_issues,
            "کامل است." if not profile_issues else "؛ ".join(profile_issues),
            True,
        )
    )
    tg_ok = bool(secrets.telegram_bot_token and cfg.notifications.chat_ids)
    checks.append(
        (
            "تلگرام",
            tg_ok,
            f"{len(cfg.notifications.chat_ids)} chat id" if tg_ok else "توکن یا chat_id خالی است.",
            True,
        )
    )
    cookie_ok = bool(
        secrets.parscoders_cookie
        or (secrets.parscoders_username and secrets.parscoders_password)
        or store.get("marketplace.name") == "mock"
    )
    checks.append(
        (
            "حساب سایت",
            cookie_ok,
            "تنظیم شده." if cookie_ok else "در bidpilot setup نام کاربری/رمز یا کوکی سایت را وارد کنید.",
            True,
        )
    )
    provider_key = secrets.llm_keys.get(cfg.llm.provider) or secrets.llm_api_key
    llm_ok = bool(cfg.llm.enabled and provider_key)
    checks.append(
        (
            "مدل زبانی",
            llm_ok,
            f"{cfg.llm.provider_title} — {cfg.llm.model}"
            if llm_ok
            else "کلید رایگان ثبت نشده؛ متن‌ها با قالب نوشته می‌شوند (کار می‌کند ولی کمتر شخصی‌سازی‌شده).",
            False,
        )
    )
    caps_ok = cfg.runtime.max_bids_per_hour <= 10 and cfg.runtime.max_bids_per_day <= 50
    checks.append(
        (
            "سقف‌های ایمنی",
            caps_ok,
            f"ساعتی {cfg.runtime.max_bids_per_hour} / روزانه {cfg.runtime.max_bids_per_day}"
            + ("" if caps_ok else " — برای شروع زیاد است."),
            False,
        )
    )
    db_ok = True
    db_detail = cfg.database_url
    try:
        Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # pragma: no cover - filesystem edge case
        db_ok, db_detail = False, str(exc)
    checks.append(("دیتابیس/پوشه داده", db_ok, db_detail, True))

    table = Table(title="Pre-flight برای حالت live")
    table.add_column("بررسی")
    table.add_column("وضعیت")
    table.add_column("توضیح")
    blocking = False
    for label, ok, detail, is_blocking in checks:
        table.add_row(label, "[green]OK[/green]" if ok else "[red]FAIL[/red]", escape(detail))
        if not ok and is_blocking:
            blocking = True
    console.print(table)

    if blocking and not force:
        console.print(
            "[bold red]تا وقتی موارد قرمز رفع نشود، حالت live فعال نمی‌شود.[/bold red]\n"
            "راهنما: docs/GO_LIVE_CHECKLIST.md"
        )
        raise typer.Exit(code=1)

    runtime = local_raw.setdefault("runtime", {}) or {}
    runtime["submit_mode"] = "live"
    local_raw["runtime"] = runtime
    local_path.write_text(_yaml.safe_dump(local_raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    # the persisted settings store wins over config.local.yaml
    store.set("runtime.submit_mode", "live")

    console.print(f"[green]حالت live فعال شد.[/green] ({local_path})")
    console.print(
        "اولین اجرای کنترل‌شده: [bold]bidpilot once --limit 1[/bold]\n"
        "سپس: [bold]bidpilot run[/bold]   |   بازگشت به آزمایشی: [bold]bidpilot go-live --off[/bold]"
    )


@app.command("telegram-whoami")
def telegram_whoami() -> None:
    """chat_id خودت را پیدا کن: به ربات پیام بده و بعد این دستور را اجرا کن."""

    async def main() -> None:
        from .config import get_secrets

        secrets = get_secrets()
        notifier = TelegramNotifier(secrets.telegram_bot_token, chat_ids=[])
        updates = await notifier.get_updates()
        if not updates:
            console.print("[yellow]هیچ پیامی پیدا نشد. اول در تلگرام به ربات /start بده.[/yellow]")
        seen = set()
        for update in updates:
            message = update.get("message") or update.get("channel_post") or {}
            chat = message.get("chat") or {}
            if chat.get("id") and chat["id"] not in seen:
                seen.add(chat["id"])
                console.print(
                    f"chat_id: [green]{chat['id']}[/green]  ({chat.get('first_name', '')} {chat.get('title', '')})"
                )
        await notifier.aclose()

    _run(main())


@app.command("telegram-test")
def telegram_test(config: str = CONFIG_OPTION) -> None:
    """ارسال یک پیام تست به همه‌ی chat_idهای تنظیم‌شده."""

    async def main() -> None:
        cfg = _load(config)
        from .config import get_secrets

        secrets = get_secrets()
        notifier = TelegramNotifier(secrets.telegram_bot_token, chat_ids=cfg.notifications.chat_ids)
        ok = await notifier.send_text(
            cfg.notifications.chat_ids[0] if cfg.notifications.chat_ids else "",
            "✅ <b>BidPilot</b> با موفقیت به تلگرام وصل شد.",
        )
        console.print("[green]OK[/green]" if ok else "[red]ناموفق[/red] — توکن/chat_id را بررسی کن.")
        await notifier.aclose()

    _run(main())


if __name__ == "__main__":  # pragma: no cover
    app()
