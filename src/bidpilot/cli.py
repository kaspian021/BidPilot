"""Command line interface (Typer + Rich)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .app import Application, build_application, configure_logging
from .config import AppConfig, get_secrets, load_config, reset_secret_cache
from .credentials import site_credentials
from .notifications.telegram import TelegramNotifier
from .onboarding import site_label
from .pricing.strategy import PricingConfig
from .proposals.generator import ProposalGenerator
from .settings_store import SettingsStore, get_store
from .sources import available_sources
from .text import to_persian_digits

log = logging.getLogger(__name__)

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


def _site_settings(cfg: AppConfig, site: str) -> dict[str, Any]:
    """YAML defaults of a site, merged with whatever the user stored for it."""
    from .sources import get_source_class
    from .sources.html_market import load_yaml_settings, merge_settings

    cls = get_source_class(site)
    defaults: dict[str, Any] = {}
    config_file = getattr(cls, "config_file", None)
    if config_file:
        defaults = load_yaml_settings(Path(config_file))
    entry = cfg.source(site)
    merged = merge_settings(defaults, (entry.settings if entry else {}) or {})
    if not merged.get("base_url") and getattr(cls, "default_base_url", ""):
        merged["base_url"] = cls.default_base_url
    return merged


def _active_site(store: SettingsStore) -> str:
    from .onboarding import DEFAULT_SITE

    return str(store.get("marketplace.name") or DEFAULT_SITE)


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
    action: str = typer.Argument("show", help="show | set | unset | path | sites | use"),
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
    if action == "sites":
        _print_sites(store)
        return
    if action == "use":
        from .onboarding import QuestionError, switch_site

        if not key:
            raise typer.BadParameter(
                "نام سایت را بده: bidpilot settings use ponisha  (ببین: bidpilot settings sites)"
            )
        try:
            active = switch_site(store, key)
        except QuestionError as exc:
            raise typer.BadParameter(str(exc)) from exc
        console.print(f"[green]سایت فعال شد:[/green] {active}")
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
    raise typer.BadParameter("دستور نامعتبر. یکی از: show | set | unset | path | sites | use")


def _print_sites(store: SettingsStore) -> None:
    """One row per registered marketplace, with the active one marked."""
    from .onboarding import sites_overview

    table = Table(title="سایت‌های فریلنسری")
    table.add_column("سایت")
    table.add_column("فعال")
    table.add_column("ورود")
    table.add_column("وضعیت حساب")
    for row in sites_overview(store):
        table.add_row(
            row["label"],
            "🟢" if row["active"] else "—",
            row["method"],
            row["summary"],
        )
    console.print(table)
    console.print(
        "[dim]عوض کردن سایت فعال:[/dim] bidpilot settings use ponisha "
        "[dim]— یا از تلگرام:[/dim] /site ponisha"
    )


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
    table.add_row(
        "پروفایل",
        f"✅ {store.get('profile.full_name')}" if store.get("profile.full_name") else "❌ وارد نشده",
    )
    table.add_row("قیمت پیشنهادی", f"{state['bid_percent']}٪ بودجه‌ی کارفرما")
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
        token = get_secrets().telegram_bot_token or store.get("telegram.bot_token")
        if not token:
            console.print(
                "[red]توکن ربات تلگرام تنظیم نشده است.[/red]\n"
                "  [bold]bidpilot setup[/bold]  → سؤال‌ها را جواب بده\n"
                "  یا: [bold]bidpilot settings set telegram.bot_token -v <TOKEN>[/bold]"
            )
            raise typer.Exit(code=1)

        service = build_bot(str(token), store=store, config_path=config)
        automation = service.automation
        automation.interval = interval
        automation.sources = source
        tasks = [asyncio.create_task(service.run_forever(), name="telegram-bot")]
        if with_runner:
            ok, message = await automation.start()
            console.print(("[green]" if ok else "[red]") + message + "[/]")
        else:
            console.print(
                "[green]ربات فعال شد.[/green] در تلگرام /start را بزن؛ "
                "برای شروع ارسال پیشنهادها دکمه‌ی [bold]🚀 شروع فعالیت[/bold] (یا /run) را بزن. "
                "اگر می‌خواهی از همان لحظه‌ی روشن‌شدن شروع کند: "
                "[bold]bidpilot bot --run[/bold]"
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
            if automation.running:
                await automation.stop()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await service.api.aclose()

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
        active_site = _active_site(store)
        creds = site_credentials(active_site, store)
        creds_ok = creds.ready or active_site == "mock"
        row(
            "حساب سایت",
            creds_ok,
            f"{site_label(active_site)} — " + (creds.summary if creds_ok else "در تلگرام /market وارد کن"),
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
def selftest(
    config: str = CONFIG_OPTION,
    source: str | None = typer.Option(None, "--source", "-s", help="فقط این منبع را تست کن"),
    limit: int = typer.Option(2, "--limit", help="حداکثر چند پروژه بررسی شود"),
    offline: bool = typer.Option(False, "--offline", help="بدون شبکه و بدون حساب: منبع آزمایشی + متن قالبی"),
    telegram_message: bool = typer.Option(
        True, "--telegram-message/--no-telegram-message", help="ارسال پیام تست به تلگرام"
    ),
    ai: bool = typer.Option(True, "--ai/--no-ai", help="فراخوانی واقعی مدل برای تست کلید"),
) -> None:
    """تست واقعی end-to-end: تلگرام، کلید هوش مصنوعی، دسترسی سایت و تولید یک پیشنهاد.

    هیچ پیشنهادی روی سایت ثبت نمی‌شود. برای تست بدون هیچ حساب واقعی از --offline استفاده کن.

    مثال‌ها:
      bidpilot selftest --offline          # فقط سلامت خود برنامه (بدون شبکه)
      bidpilot selftest                    # تست واقعی با همان تنظیمات ذخیره‌شده
      bidpilot selftest -s parscoders      # فقط پارس‌کدرز
      bidpilot selftest -s ponisha         # سایت دیگر، با حساب خودش
    """
    from .bot import BotAPIError, TelegramAPI
    from .config import Secrets, SourceConfig
    from .llm_providers import resolve_chain, verify_provider
    from .sources import available_sources

    if source and source not in available_sources():
        raise typer.BadParameter(f"سایت «{source}» ثبت نشده است. یکی از: {', '.join(available_sources())}")

    OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
    results: list[tuple[str, str, str]] = []

    def add(label: str, status: str, detail: str = "") -> None:
        results.append((label, status, detail))

    async def main() -> None:
        cfg = _load(config)
        store = get_store()
        secrets = get_secrets()

        if offline:
            cfg.sources = [SourceConfig(type="mock", settings={})]
            cfg.llm.enabled = False
            cfg.notifications.enabled = False

        add(
            "تنظیمات ذخیره‌شده",
            OK if store.exists else WARN,
            str(store.path) if store.exists else "خالی — با bidpilot setup پر کن",
        )

        # ---------------------------------------------------------- telegram
        if offline:
            add("تلگرام", SKIP, "در حالت offline بررسی نمی‌شود")
        elif not secrets.telegram_bot_token:
            add("تلگرام", FAIL, "توکن ربات ثبت نشده — bidpilot setup")
        elif not cfg.notifications.chat_ids:
            add("تلگرام", WARN, "توکن هست ولی chat_id ثبت نشده — در ربات /start بزن")
        else:
            api = TelegramAPI(str(secrets.telegram_bot_token))
            try:
                me = await api.get_me()
                add("تلگرام (توکن)", OK, f"@{me.get('username', '?')}")
                if telegram_message:
                    sent = 0
                    for chat_id in cfg.notifications.chat_ids:
                        try:
                            await api.send_message(
                                chat_id,
                                "✅ <b>BidPilot</b> — تست اتصال موفق بود. "
                                "این پیام فقط برای اطمینان از تنظیمات است.",
                            )
                            sent += 1
                        except BotAPIError as exc:
                            add("تلگرام (ارسال)", FAIL, f"{chat_id}: {exc}")
                    if sent:
                        add("تلگرام (ارسال)", OK, f"پیام تست به {sent} چت رسید")
            except BotAPIError as exc:
                add("تلگرام", FAIL, f"توکن نامعتبر است یا دسترسی به API نیست: {exc}")
            finally:
                await api.aclose()

        # ---------------------------------------------------------------- ai
        chain = resolve_chain(cfg.llm, secrets.llm_keys, secrets.llm_api_key)
        if offline or not ai:
            add("هوش مصنوعی", SKIP, "در این حالت فراخوانی نمی‌شود")
        elif not chain:
            add(
                "هوش مصنوعی",
                WARN,
                "کلید رایگان ثبت نشده — متن‌ها با قالب نوشته می‌شوند (bidpilot setup)",
            )
        else:
            working = False
            for provider in chain:
                try:
                    ok, message = await verify_provider(provider)
                except Exception as exc:  # network / DNS
                    ok, message = False, f"خطای شبکه: {exc}"
                add(f"هوش مصنوعی: {provider.id}", OK if ok else FAIL, message)
                if ok:
                    working = True
                    break
            if not working:
                add(
                    "هوش مصنوعی",
                    WARN,
                    "هیچ کلیدی در دسترس نبود؛ برنامه با قالب فارسی ادامه می‌دهد.",
                )

        # --------------------------------------------------------- marketplace
        application = build_application(cfg)
        generated: tuple[Any, Any, Any] | None = None
        delivery = int(cfg.pricing.get("delivery_days", 14))
        try:
            if source:
                name = source
                stored = get_store().get("marketplace.name")
                if stored and stored != source:
                    add(
                        "سایت فعال",
                        WARN,
                        f"سایت فعال «{stored}» است؛ این تست فقط «{source}» را بررسی می‌کند",
                    )
                else:
                    add("سایت فعال", OK, source)
            else:
                name = _active_site(get_store())
                if name not in application.sources:
                    name = next(iter(application.sources), None)
            if not name:
                add("سایت", FAIL, "هیچ منبعی فعال نیست — /market یا bidpilot setup")
            else:
                src = application.sources.get(name)
                if src is None:
                    from .sources import create_source

                    entry = cfg.source(name)
                    src = create_source(
                        name,
                        (entry.settings if entry else {}),
                        cfg,
                        Secrets() if offline else secrets,
                    )
                    application.sources[name] = src
                from .credentials import site_credentials

                creds = site_credentials(name)
                add(
                    f"حساب {name}",
                    OK if creds.ready else WARN,
                    creds.summary if creds.ready else f"{creds.summary} — در تلگرام /market",
                )
                verify = getattr(src, "verify_session", None)
                if verify is not None and not offline:
                    try:
                        session_ok, session_detail = await verify()
                    except Exception as exc:  # network problems were reported above
                        session_ok, session_detail = False, f"خطا: {exc}"
                    add("ورود به حساب", OK if session_ok else FAIL, session_detail)

                try:
                    ok, detail = await src.healthcheck()
                except Exception as exc:
                    ok, detail = False, f"خطا: {exc}"
                add(f"سایت: {name}", OK if ok else FAIL, detail)

                projects = await src.fetch_projects() if ok else []
                add(
                    "دریافت پروژه",
                    OK if projects else WARN,
                    f"{len(projects)} پروژه در صفحه اول" if projects else "پروژه‌ای پیدا نشد",
                )

                if projects:
                    flt = application.filter_preview()
                    pricing = PricingConfig(**cfg.pricing).build()
                    for project in projects[: max(1, limit * 5)]:
                        if not flt.evaluate(project):
                            continue
                        price = pricing.price(project, project.budget)
                        if price is None:
                            continue
                        proposal = await application.generator.generate(  # type: ignore[union-attr]
                            project, price.amount, delivery
                        )
                        generated = (project, price, proposal)
                        break
                    if generated is None:
                        add("تولید پیشنهاد", WARN, "هیچ پروژه‌ای از فیلترها/قیمت‌گذاری عبور نکرد")
        finally:
            await application.close()

        if generated is not None and not offline:
            # only a test of the *active* site unlocks live mode for it
            if not source or source == _active_site(get_store()):
                _mark_safe_test("selftest")
        if generated is not None:
            project, price, proposal = generated
            source_name = proposal.generated_by
            add(
                "تولید پیشنهاد",
                OK,
                f"{price.amount:,} تومان · {delivery_days_label(cfg)} · منبع متن: {source_name}",
            )

        table = Table(title="Self-test — BidPilot")
        table.add_column("بررسی")
        table.add_column("وضعیت")
        table.add_column("توضیح")
        icon = {
            OK: "[green]✅ OK[/green]",
            WARN: "[yellow]⚠️ هشدار[/yellow]",
            FAIL: "[red]❌ خطا[/red]",
            SKIP: "[dim]⏭ رد شد[/dim]",
        }
        for label, status, detail in results:
            table.add_row(escape(label), icon[status], escape(detail))
        console.print(table)

        if generated is not None:
            project, price, proposal = generated
            console.print(
                Panel(
                    escape(proposal.text),
                    title=f"نمونه پیشنهاد | {escape(project.title)} | {price.amount:,} تومان",
                    subtitle=f"{proposal.generated_by} — {project.url}",
                    border_style="cyan",
                )
            )

        failures = [r for r in results if r[1] == FAIL]
        if failures:
            console.print(
                "\n[bold red]چند مورد ناموفق بود.[/bold red] "
                "پیام هر ردیف را بخوان یا [bold]bidpilot setup[/bold] را دوباره اجرا کن."
            )
            raise typer.Exit(code=1)
        if offline:
            console.print(
                "\n[bold green]خود برنامه سالم است.[/bold green] "
                "حالا برای تست واقعی (تلگرام + کلید AI + حساب سایت):\n"
                "  [bold]bidpilot setup[/bold]  سپس  [bold]bidpilot selftest[/bold]"
            )
        else:
            console.print(
                "\n[bold green]آماده‌ی تست واقعی است.[/bold green]\n"
                "  گام بعد: [bold]bidpilot once --dry-run --limit 2[/bold]  (بدون ارسال)\n"
                "  بعد: [bold]bidpilot bot --run[/bold]  یا  [bold]bidpilot go-live[/bold]"
            )

    _run(main())


def _mark_safe_test(kind: str) -> None:
    """A real proposal was generated without sending it -> unlock 🔴 live in /run."""
    try:
        from .onboarding import mark_preview_ok

        mark_preview_ok(get_store(), "dry_run" if "dry" in kind else "preview", note=kind)
    except Exception as exc:  # a read-only store must not break the command
        log.debug("Could not store the safe-test proof: %s", exc)


def delivery_days_label(cfg: AppConfig) -> str:
    return f"{int(cfg.pricing.get('delivery_days', 14))} روز"


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
        if any(r.dry_run for r in reports) and not any(r.submitted for r in reports):
            _mark_safe_test("once --dry-run")
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
        name = source or _active_site(get_store())
        if name not in application.sources:
            name = next(iter(application.sources), None)
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
    site: str | None = typer.Option(None, "--site", "-s", help="سایت (پیش‌فرض: سایت فعال)"),
    config: str = CONFIG_OPTION,
    save: bool = typer.Option(True, "--save/--no-save", help="ذخیره‌ی HTML در data/debug"),
) -> None:
    """دیباگ سلکتورها: کدام سلکتور چند مورد پیدا می‌کند؟ (وقتی ظاهر سایت عوض می‌شود)

    مثال:
      bidpilot inspect                      # سایت فعال
      bidpilot inspect -s ponisha           # سایت دیگر، با همان کوکی خودش
      bidpilot inspect -u https://ponisha.ir/search/projects
      bidpilot inspect -f ~/Downloads/project.html
    """

    async def main() -> None:
        import httpx
        from bs4 import BeautifulSoup

        from .credentials import site_credentials
        from .sources import available_sources, get_source_class

        cfg = _load(config)
        active = site or _active_site(get_store())
        if active not in available_sources():
            raise typer.BadParameter(
                f"سایت «{active}» ثبت نشده است. یکی از: {', '.join(available_sources())}"
            )
        defaults = _site_settings(cfg, active)
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
            cookies = site_credentials(active).cookies
            if cookies:
                console.print(f"[dim]با کوکی ذخیره‌شده‌ی {active} درخواست می‌شود.[/dim]")
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
        console.print(f"[bold]سایت:[/bold] {active}")
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

        from .sources.html_parser import fallback_cards, parse_list_page

        id_regex = list_cfg.get("id_regex", r"/projects?/(\d+)")
        import re as _re

        links = [a["href"] for a in soup.find_all("a", href=True) if _re.search(id_regex, a["href"])]
        console.print(f"لینک‌های پروژه‌مانند: [bold]{len(links)}[/bold]")
        for link in links[:5]:
            console.print(f"  • {link}")

        detected = parse_list_page(html, list_cfg, active, str(defaults.get("base_url") or ""))
        console.print(f"\nتشخیص خودکار کارت‌ها: [bold]{len(detected)}[/bold] پروژه")
        if not detected and fallback_cards(soup, list_cfg):
            console.print(
                "[yellow]سلکتورها چیزی پیدا نکردند؛ فقط fallback محتوایی جواب داد — "
                "یک سلکتور کارت از DevTools بردار.[/yellow]"
            )
        for project in detected[:3]:
            console.print(f"  • {escape(project.title)}  →  {escape(project.url)}")

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

        config_file = getattr(get_source_class(active), "config_file", None)
        console.print(
            "\n[dim]اگر همه‌ی سلکتورها ۰ بودند: سلکتور درست را از DevTools بردار و در "
            f"{config_file or 'config.yaml'} همان سایت جایگزین کن.[/dim]"
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
    active_site = _active_site(store)
    cookie_ok = site_credentials(active_site, store).ready or active_site == "mock"
    checks.append(
        (
            f"حساب سایت ({active_site})",
            cookie_ok,
            "تنظیم شده."
            if cookie_ok
            else "در تلگرام /market یا bidpilot setup کوکی/نام کاربری سایت را وارد کنید.",
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
        "سپس: در تلگرام دکمه‌ی [bold]🚀 شروع فعالیت[/bold] (یا /run) — "
        "یا از ترمینال: [bold]bidpilot run[/bold]\n"
        "بازگشت به آزمایشی: [bold]bidpilot go-live --off[/bold] یا /mode"
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
