"""Taiwan B2B CRM -- command line entry point.

    python main.py crawl      # collect public company data
    python main.py verify     # re-clean and re-verify stored contacts
    python main.py export     # write Excel / CSV / JSON
    python main.py gui        # open the desktop application

``python main.py --help`` lists everything else (import, backup, restore,
stats, duplicates, sources, gmail).
"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# Running "python main.py" from another directory must still resolve packages.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Traditional-Chinese Windows gives the console cp950 (Big5). Printing a "✓" --
# or any of the box-drawing characters rich uses -- raises UnicodeEncodeError
# and takes the whole command down with a traceback. "python main.py security"
# is documented as the thing to run before pushing to git, so it crashing on
# the audience's default console is not a cosmetic problem.
for _stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(_stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover - redirected streams
            pass

from core.config import get_config  # noqa: E402
from core.constants import PROJECT_NAME, VERSION, LogCategory, RecordStatus  # noqa: E402
from core.errors import CRMError  # noqa: E402
from core.logging_setup import get_logger, setup_logging  # noqa: E402
from core.repo import (  # noqa: E402
    git_tracked_files,
    git_untracked_unignored_files,
)
from core.schemas import CompanyFilter  # noqa: E402

console = Console()

app = typer.Typer(
    name="taiwan-b2b-crm",
    help=f"{PROJECT_NAME} v{VERSION} - collect, verify and manage public B2B contact data.",
    add_completion=False,
    no_args_is_help=True,
)


def _bootstrap(create_schema: bool = True):
    """Load config, install logging, ensure directories and schema exist."""
    from core.tls import install_os_trust_store
    from database.session import init_db

    config = get_config()
    config.ensure_directories()
    setup_logging(config)
    # Before any HTTPS connection: many Taiwanese sites use TWCA certificates
    # that Python's bundled trust store rejects but the OS accepts.
    install_os_trust_store()
    if create_schema:
        init_db()
    return config


def _fail(message: str) -> None:
    console.print(f"[bold red]Error:[/bold red] {message}")
    raise typer.Exit(code=1)


def _crawl_url(
    url: str,
    save_as: str | None,
    max_pages: int | None,
    config,
    from_page: int | None = None,
    to_page: int | None = None,
) -> None:
    """Analyse an arbitrary URL, show what was detected, then crawl it."""
    from core.config import save_custom_source
    from crawler.discover import discover
    from crawler.pipeline import CrawlPipeline

    try:
        with console.status(f"[bold green]分析 {url} ..."):
            result = discover(url, config)
    except CRMError as exc:
        _fail(str(exc))

    if not result.list_selector:
        for note in result.notes:
            console.print(f"[yellow]{note}[/yellow]")
        _fail("這個頁面找不到可辨識的公司清單。請改用 GUI 的自訂網址精靈手動指定選擇器。")

    table = Table(title=f"偵測結果：{result.item_count} 筆資料區塊")
    table.add_column("欄位", style="cyan")
    table.add_column("CSS 選擇器", overflow="fold")
    table.add_column("命中率", justify="right")
    table.add_column("範例", overflow="fold")
    for name, guess in result.fields.items():
        table.add_row(
            name, guess.selector, f"{guess.hit_rate:.0%}",
            guess.samples[0] if guess.samples else "-",
        )
    console.print(table)
    for note in result.notes:
        console.print(f"[yellow]提醒：{note}[/yellow]")

    if not result.ok:
        _fail("沒有偵測到公司名稱欄位，無法自動爬取。")

    name = save_as or urlsplit(result.url).netloc.replace(".", "-") or "custom"
    source_config = result.to_source_config(name)

    if from_page is not None or to_page is not None:
        source_config = source_config.model_copy(
            update={
                "page_start": from_page if from_page is not None else source_config.page_start,
                "page_end": to_page,
            }
        )
        console.print(
            f"[dim]頁碼範圍：第 {source_config.page_start} 頁"
            f"{f' 至第 {to_page} 頁' if to_page else ' 起'}[/dim]"
        )

    if save_as:
        path = save_custom_source(source_config)
        console.print(f"[green]來源已儲存為 {name}[/green] -> {path}")

    with CrawlPipeline(config) as pipeline:
        summary = pipeline.run_source_config(source_config, max_pages=max_pages)

    console.print(
        f"[green]完成[/green]：{summary.status}，共 {summary.pages_crawled} 頁、"
        f"找到 {summary.records_found} 筆、新增 {summary.records_new} 筆、"
        f"重複 {summary.records_duplicate} 筆"
    )
    if summary.error:
        console.print(f"[red]{summary.error}[/red]")


# --------------------------------------------------------------------- crawl


@app.command()
def crawl(
    source: Optional[str] = typer.Option(
        None, "--source", "-s", help="Source name from config.yaml. Omit to run all enabled."
    ),
    url: Optional[str] = typer.Option(
        None, "--url", "-u", help="Analyse this URL and crawl it, no config needed."
    ),
    save_as: Optional[str] = typer.Option(
        None, "--save-as", help="With --url: save the detected recipe under this name."
    ),
    max_pages: Optional[int] = typer.Option(
        None, "--max-pages", "-p", help="最多爬幾頁。"
    ),
    from_page: Optional[int] = typer.Option(
        None, "--from-page", help="從第幾頁開始（含）。"
    ),
    to_page: Optional[int] = typer.Option(
        None, "--to-page", help="爬到第幾頁為止（含）。"
    ),
    list_sources: bool = typer.Option(
        False, "--list", "-l", help="List configured sources and exit."
    ),
) -> None:
    """Crawl public company data from the configured sources, or from a URL."""
    config = _bootstrap()

    if from_page is not None and to_page is not None and to_page < from_page:
        _fail(f"--to-page ({to_page}) 不能小於 --from-page ({from_page})")

    if url:
        _crawl_url(url, save_as, max_pages, config, from_page, to_page)
        return

    if list_sources:
        table = Table(title="Configured crawl sources")
        table.add_column("Name", style="cyan")
        table.add_column("Type")
        table.add_column("Enabled")
        table.add_column("Start URL", overflow="fold")
        for item in config.crawler.sources:
            table.add_row(
                item.name,
                item.type,
                "[green]yes[/green]" if item.enabled else "[dim]no[/dim]",
                item.start_url or "(bundled fixture)",
            )
        console.print(table)
        return

    if not config.crawler.enabled_sources() and source is None:
        _fail("no crawl sources are enabled. Enable one in config.yaml, or use --source.")

    from crawler.pipeline import crawl as run_crawl

    def progress(name: str, page: int, stored: int, total: int) -> None:
        console.print(f"  [dim]{name}[/dim] page {page}/{total} -- {stored} new so far")

    try:
        with console.status("[bold green]Crawling..."):
            summaries = run_crawl(source, config, progress=progress, max_pages=max_pages)
    except CRMError as exc:
        _fail(str(exc))

    table = Table(title="Crawl results")
    for column in ("Source", "Status", "Pages", "Found", "New", "Merged", "Dupes", "Rejected"):
        table.add_column(column)
    for summary in summaries:
        colour = {"Success": "green", "Partial": "yellow"}.get(summary.status, "red")
        table.add_row(
            summary.source,
            f"[{colour}]{summary.status}[/{colour}]",
            str(summary.pages_crawled),
            str(summary.records_found),
            str(summary.records_new),
            str(summary.records_updated),
            str(summary.records_duplicate),
            str(summary.records_invalid),
        )
    console.print(table)
    for summary in summaries:
        if summary.error:
            console.print(f"[red]{summary.source}:[/red] {summary.error}")


# -------------------------------------------------------------------- verify


@app.command()
def verify(
    only_missing: bool = typer.Option(
        False, "--only-missing", help="Verify only records never checked before."
    ),
    no_renormalize: bool = typer.Option(
        False, "--no-renormalize", help="Verify emails only; leave other fields untouched."
    ),
) -> None:
    """Re-normalize stored records and re-check their email addresses."""
    _bootstrap()

    from core.constants import EmailVerdict
    from database.repository import CompanyRepository
    from database.session import session_scope
    from verifier.service import VerificationService

    with session_scope() as session:
        repo = CompanyRepository(session)
        targets = repo.all()
        if only_missing:
            targets = [c for c in targets if c.email_verdict == EmailVerdict.UNKNOWN.value]

        if not targets:
            console.print("[yellow]Nothing to verify.[/yellow]")
            return

        service = VerificationService(session)
        with console.status(f"[bold green]Verifying {len(targets)} records..."):
            summary = service.run(targets, renormalize=not no_renormalize)

    table = Table(title="Verification results")
    table.add_column("Metric", style="cyan")
    table.add_column("Count", justify="right")
    for label, value in (
        ("Checked", summary.checked),
        ("Valid", summary.valid),
        ("No MX record", summary.no_mx),
        ("Invalid syntax", summary.invalid_syntax),
        ("Disposable", summary.disposable),
        ("No email", summary.empty),
        ("Records updated", summary.updated),
    ):
        table.add_row(label, str(value))
    console.print(table)


# -------------------------------------------------------------------- export


@app.command()
def export(
    format: str = typer.Option("excel", "--format", "-f", help="excel | csv | json"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Target file or folder."),
    search: Optional[str] = typer.Option(None, "--search", help="Full-text filter."),
    industry: Optional[str] = typer.Option(None, "--industry", help="Filter by industry."),
    stage: Optional[str] = typer.Option(None, "--stage", help="Filter by pipeline stage."),
    tag: Optional[list[str]] = typer.Option(None, "--tag", help="Filter by tag (repeatable)."),
    with_email: bool = typer.Option(False, "--with-email", help="Only rows that have an email."),
    active_only: bool = typer.Option(False, "--active-only", help="Exclude archived records."),
    limit: Optional[int] = typer.Option(None, "--limit", help="Cap the number of rows."),
    all_formats: bool = typer.Option(False, "--all", help="Write Excel, CSV and JSON."),
) -> None:
    """Export companies to Excel, CSV or JSON."""
    config = _bootstrap()

    from exporter.service import export_all_formats, export_companies

    criteria = CompanyFilter(
        text=search,
        industry=industry,
        stages=[stage] if stage else [],
        tags=list(tag) if tag else [],
        has_email=True if with_email else None,
        statuses=[RecordStatus.ACTIVE.value] if active_only else [],
        limit=limit,
    )

    try:
        if all_formats:
            written = export_all_formats(criteria, config)
            for name, path in written.items():
                console.print(f"[green]{name}[/green] -> {path}")
            return
        path, count = export_companies(format, output, criteria, config)
    except CRMError as exc:
        _fail(str(exc))

    console.print(f"[green]Exported {count} companies[/green] -> {path}")


# -------------------------------------------------------------------- import


@app.command("import")
def import_data(
    file: Path = typer.Argument(..., help="CSV, Excel or JSON file to import."),
    label: Optional[str] = typer.Option(None, "--label", help="Value stored in the source column."),
) -> None:
    """Import companies from a spreadsheet, applying the full cleaning pipeline."""
    config = _bootstrap()

    from exporter.importer import import_file

    try:
        summary = import_file(file, label, config)
    except CRMError as exc:
        _fail(str(exc))

    console.print(
        Panel(
            f"Rows read: {summary.rows_read}\n"
            f"New companies: [green]{summary.records_new}[/green]\n"
            f"Merged into existing: {summary.records_merged}\n"
            f"Duplicates collapsed: {summary.records_duplicate}\n"
            f"Rejected: [yellow]{summary.records_invalid}[/yellow]",
            title=f"Imported {Path(summary.file).name}",
        )
    )
    if summary.unmapped_columns:
        console.print(
            "[dim]Kept as free-form fields: "
            f"{', '.join(summary.unmapped_columns)}[/dim]"
        )


# ----------------------------------------------------------------------- gui


@app.command()
def gui() -> None:
    """Open the desktop application.

    The interface is PySide6 (``gui_qt/``). It replaced a customtkinter build
    that repainted a canvas per widget: switching to the mail page cost 205ms
    there against roughly 6ms here. The backend and the ``controllers`` layer
    are untouched by that change -- the controllers were always the seam.
    """
    _bootstrap()

    try:
        from gui_qt.app import run_gui_qt
    except ImportError as exc:
        _fail(f"the GUI needs PySide6: {exc}")

    get_logger(LogCategory.GUI).info("GUI starting")
    run_gui_qt()


# --------------------------------------------------------------------- stats


@app.command()
def stats() -> None:
    """Show a summary of what is in the database."""
    _bootstrap()

    from database.repository import StatsRepository
    from database.session import session_scope

    with session_scope() as session:
        data = StatsRepository(session).dashboard()

    table = Table(title=f"{PROJECT_NAME} - database summary")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right")
    for label, value in (
        ("Total companies", data.total_companies),
        ("With an email", data.total_emails),
        ("Verified emails", data.verified_emails),
        ("Contacts", data.total_contacts),
        ("Added today", data.new_today),
        ("Added this week", data.new_this_week),
        ("Suspected duplicates", data.duplicates),
        ("Follow-ups due", data.follow_ups_due),
    ):
        table.add_row(label, str(value))
    console.print(table)

    if data.by_stage:
        stage_table = Table(title="Pipeline")
        stage_table.add_column("Stage", style="cyan")
        stage_table.add_column("Companies", justify="right")
        for name, count in data.by_stage.items():
            stage_table.add_row(name, str(count))
        console.print(stage_table)

    if data.last_crawl:
        last = data.last_crawl
        console.print(
            f"[dim]Last crawl: {last.source} [{last.status}] "
            f"{last.started_at:%Y-%m-%d %H:%M} - "
            f"{last.records_new} new, {last.records_duplicate} duplicates[/dim]"
        )


# ---------------------------------------------------------------- duplicates


@app.command()
def duplicates(
    merge: bool = typer.Option(False, "--merge", help="Merge each group into its oldest record."),
) -> None:
    """List (or merge) records that look like duplicates."""
    _bootstrap()

    from database.repository import CompanyRepository
    from database.session import session_scope

    with session_scope() as session:
        repo = CompanyRepository(session)
        groups = repo.find_duplicate_groups()

        if not groups:
            console.print("[green]No duplicates found.[/green]")
            return

        table = Table(title=f"{len(groups)} duplicate group(s)")
        table.add_column("Keep", style="green")
        table.add_column("Duplicates", style="yellow")
        table.add_column("Company")
        table.add_column("Email", overflow="fold")
        for group in groups:
            keeper, *rest = group
            table.add_row(
                str(keeper.id),
                ", ".join(str(c.id) for c in rest),
                keeper.company_name,
                keeper.email or "-",
            )
        console.print(table)

        if not merge:
            console.print("[dim]Re-run with --merge to combine them.[/dim]")
            return

        merged = 0
        for group in groups:
            keeper, *rest = group
            repo.merge(keeper.id, [c.id for c in rest])
            merged += len(rest)
        console.print(f"[green]Merged {merged} duplicate record(s).[/green]")


# -------------------------------------------------------------------- backup


@app.command()
def backup(
    restore: Optional[str] = typer.Option(
        None, "--restore", help="Restore from this backup file name and exit."
    ),
    list_only: bool = typer.Option(False, "--list", "-l", help="List existing backups."),
    prune: bool = typer.Option(False, "--prune", help="Delete backups past their retention."),
) -> None:
    """Create, list, prune or restore database backups."""
    config = _bootstrap()

    from database.backup import create_backup, list_backups, prune_backups, restore_backup
    from database.session import reset_engine

    try:
        if restore:
            reset_engine()  # release the file handle before overwriting it
            path = restore_backup(restore, config)
            console.print(f"[green]Database restored[/green] from {restore} -> {path}")
            return

        if prune:
            removed = prune_backups(config)
            console.print(f"[green]Pruned {len(removed)} backup(s).[/green]")
            return

        if not list_only:
            created = create_backup("manual", config)
            console.print(f"[green]Backup created:[/green] {created.name} ({created.size_mb:.2f} MB)")

        entries = list_backups(config)
    except CRMError as exc:
        _fail(str(exc))

    if not entries:
        console.print("[yellow]No backups yet.[/yellow]")
        return

    table = Table(title="Backups")
    table.add_column("File", style="cyan")
    table.add_column("Kind")
    table.add_column("Created")
    table.add_column("Size", justify="right")
    for entry in entries:
        table.add_row(
            entry.name, entry.kind, f"{entry.created_at:%Y-%m-%d %H:%M}", f"{entry.size_mb:.2f} MB"
        )
    console.print(table)


# --------------------------------------------------------------------- gmail


@app.command("gmail")
def gmail_harvest(
    query: Optional[str] = typer.Option(None, "--query", help="IMAP search, e.g. 'UNSEEN'."),
    limit: Optional[int] = typer.Option(None, "--limit", help="Maximum messages to scan."),
) -> None:
    """Harvest B2B contacts from your own Gmail inbox (read-only)."""
    config = _bootstrap()

    if not config.gmail.enabled:
        _fail("Gmail is disabled. Set gmail.enabled: true in config.yaml.")

    from gmail.harvester import harvest_inbox

    try:
        with console.status("[bold green]Reading mailbox..."):
            summary = harvest_inbox(query, limit, config)
    except CRMError as exc:
        _fail(str(exc))

    console.print(
        Panel(
            f"Messages scanned: {summary.messages_scanned}\n"
            f"Skipped (free-mail sender): {summary.messages_skipped}\n"
            f"New companies: [green]{summary.records_new}[/green]\n"
            f"Merged into existing: {summary.records_merged}\n"
            f"Contacts created: {summary.contacts_created}",
            title="Gmail harvest",
        )
    )


# -------------------------------------------------------------------- config


@app.command("check")
def check_config() -> None:
    """Validate config.yaml and report what the app will do."""
    try:
        config = _bootstrap(create_schema=False)
    except CRMError as exc:
        _fail(str(exc))

    table = Table(title="Configuration")
    table.add_column("Setting", style="cyan")
    table.add_column("Value", overflow="fold")
    table.add_row("Database", config.database.resolved_url)
    table.add_row("Log directory", str(config.logging.resolved_dir))
    table.add_row("Output directory", str(config.exporter.resolved_output_dir))
    table.add_row("Backup directory", str(config.backup.resolved_dir))
    table.add_row("Crawl engine", config.crawler.engine)
    table.add_row("User-Agent", config.crawler.resolved_user_agent())
    table.add_row("Respect robots.txt", "yes" if config.crawler.respect_robots else "NO")
    table.add_row("Crawl delay", f"{config.crawler.delay_seconds}s (+ up to {config.crawler.delay_jitter}s)")
    table.add_row("MX checking", "on" if config.verifier.check_mx else "off")
    table.add_row(
        "Field encryption",
        "on" if config.database.encrypt else "off (personal data stored in clear)",
    )
    table.add_row("Gmail", "enabled" if config.gmail.enabled else "disabled")
    table.add_row(
        "Enabled sources",
        ", ".join(s.name for s in config.crawler.enabled_sources()) or "(none)",
    )
    console.print(table)

    if not config.crawler.respect_robots:
        console.print(
            "[bold red]robots.txt checking is disabled.[/bold red] "
            "Only do this for sites you own."
        )
    console.print("[green]Configuration is valid.[/green]")


def _export_database_key(out: Optional[Path]) -> None:
    """印出或寫出資料庫金鑰，並說明它為什麼重要。"""
    from core.config import PROJECT_ROOT
    from core.crypto import export_key

    try:
        key = export_key()
    except CRMError as exc:
        _fail(f"{exc}（尚未加密過任何資料時沒有金鑰可匯出）")

    if out is not None:
        target = out.expanduser().resolve()
        # 寫進專案資料夾等於把金鑰放在資料庫旁邊，兩個一起被複製走就白加密了，
        # 而且遲早會有人不小心 commit 進去。
        if target == PROJECT_ROOT or PROJECT_ROOT in target.parents:
            _fail(
                f"拒絕把金鑰寫進專案資料夾（{target}）。"
                "金鑰和資料庫放在一起就失去加密的意義了，請改存到隨身碟或密碼管理員。"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(key + "\n", encoding="utf-8")
        console.print(f"[green]金鑰已寫入 {target}[/green]")
    else:
        console.print(Panel(key, title="資料庫金鑰", border_style="yellow"))

    console.print(
        "\n[bold yellow]把這串字收好。[/bold yellow]"
        "它是唯一能解開 data/crm.db 與 backups/ 內所有備份的東西。\n"
        "建議存到密碼管理員，或印出來放抽屜——[bold]不要和資料庫放在同一個地方[/bold]。\n"
        "換電腦或重灌後，執行 [cyan]python main.py encrypt --import-key[/cyan] 貼回去即可。"
    )


def _import_database_key(force: bool) -> None:
    """把先前匯出的金鑰貼回保管庫。

    刻意用互動式輸入而不是命令列參數：金鑰打在指令裡會留在 shell 的歷史紀錄中。
    """
    from core.crypto import import_key

    console.print("請貼上先前用 --export-key 匯出的金鑰（輸入不會顯示）：")
    value = typer.prompt("金鑰", hide_input=True)

    try:
        import_key(value, force=force)
    except CRMError as exc:
        _fail(str(exc))

    console.print("[green]金鑰已匯入系統憑證保管庫。[/green]")
    console.print("接著把 data/crm.db（或一份 backups/ 內的備份）複製回來，即可正常開啟。")


@app.command("encrypt")
def encrypt_status(
    export_key: bool = typer.Option(
        False, "--export-key", help="印出資料庫金鑰，讓你自己保管一份。"
    ),
    import_key: bool = typer.Option(
        False, "--import-key", help="貼回先前匯出的金鑰（換電腦、重灌後用）。"
    ),
    out: Optional[Path] = typer.Option(
        None, "--out", help="把匯出的金鑰寫到檔案，而不是印在畫面上。"
    ),
    force: bool = typer.Option(
        False, "--force", help="匯入時覆蓋保管庫中已有的另一把金鑰。"
    ),
) -> None:
    """Show whether personal-data columns are encrypted, and manage the key.

    ``_bootstrap`` already ran the conversion (it happens on every start-up),
    so the plain form reports the result rather than performing it. Change
    ``database.encrypt`` in config.yaml and run this to convert.
    """
    if export_key and import_key:
        _fail("--export-key 與 --import-key 不能同時使用")

    if import_key:
        _import_database_key(force)
        return

    _bootstrap()

    if export_key:
        _export_database_key(out)
        return

    from database.encryption import encrypted_columns, status
    from database.session import get_engine

    try:
        report = status(get_engine())
    except CRMError as exc:
        _fail(str(exc))

    table = Table(title="資料庫加密")
    table.add_column("項目", style="cyan")
    table.add_column("狀態", overflow="fold")
    table.add_row("設定 (database.encrypt)", "開啟" if report.configured else "關閉")
    table.add_row("此環境可加密", "是" if report.usable else "否（缺 cryptography 或系統憑證保管庫）")
    table.add_row("目前狀態", report.describe())
    table.add_row("已加密的欄位值", str(report.encrypted_values))
    table.add_row("明文的欄位值", str(report.plaintext_values))
    for table_name, columns in sorted(encrypted_columns().items()):
        table.add_row(f"加密欄位／{table_name}", ", ".join(columns))
    console.print(table)

    if report.active:
        console.print(
            "[yellow]金鑰存在系統憑證保管庫，不在專案資料夾內。"
            "換電腦或清除認證管理員後，加密欄位將無法還原——備份資料庫時請一併確認。[/yellow]"
        )
    if not report.fully_converted:
        console.print(
            f"[bold red]還有 {report.pending} 個欄位值與設定不一致。[/bold red] "
            "請確認系統憑證保管庫可用後重新執行。"
        )


@app.command()
def schedule(
    run_now: bool = typer.Option(False, "--now", help="Run one pass immediately and exit."),
    status_only: bool = typer.Option(False, "--status", help="Show the schedule and exit."),
) -> None:
    """Run scheduled crawls in the foreground until you press Ctrl+C.

    Use this when you want unattended collection without keeping the GUI open;
    pair it with Windows Task Scheduler at boot for a headless setup.
    """
    config = _bootstrap()

    from core.scheduler import CrawlScheduler, load_state, next_run_after

    settings = config.scheduler
    state = load_state(config)

    table = Table(title="排程設定")
    table.add_column("項目", style="cyan")
    table.add_column("值")
    table.add_row("已啟用", "是" if settings.enabled else "否")
    table.add_row("模式", settings.mode)
    table.add_row(
        "時間",
        settings.at if settings.mode == "daily" else f"每 {settings.every_minutes} 分鐘",
    )
    table.add_row("來源", ", ".join(settings.sources) or "（所有已啟用的來源）")
    table.add_row("爬完自動驗證", "是" if settings.verify_after_crawl else "否")
    table.add_row(
        "上次執行",
        f"{state.last_run:%Y-%m-%d %H:%M}（{state.last_status}）" if state.last_run else "從未執行",
    )
    if settings.enabled:
        table.add_row(
            "下次執行",
            f"{next_run_after(datetime.now(), settings, state.last_run):%Y-%m-%d %H:%M}",
        )
    console.print(table)

    if status_only:
        return

    scheduler = CrawlScheduler(config)

    if run_now:
        console.print("[green]立即執行一次排程任務...[/green]")
        scheduler._run_job()  # noqa: SLF001 - deliberate one-shot entry point
        console.print("[green]完成。[/green]")
        return

    if not settings.enabled:
        _fail("排程未啟用。請在 config.yaml 把 scheduler.enabled 設為 true，或加上 --now 執行一次。")

    scheduler.start()
    console.print("[green]排程執行中，按 Ctrl+C 結束。[/green]")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        console.print("\n[yellow]正在停止排程...[/yellow]")
        scheduler.stop()
        console.print("[green]已停止。[/green]")


@app.command()
def enrich(
    limit: Optional[int] = typer.Option(None, "--limit", "-n", help="最多處理幾家公司。"),
) -> None:
    """對「有網址、沒信箱」的公司，到它官網補抓公開刊登的信箱。

    每家公司都是不同網域，因此會逐一檢查各自的 robots.txt。
    """
    config = _bootstrap()

    from crawler.enrich import enrich_missing_emails

    def progress(index: int, total: int, name: str) -> None:
        console.print(f"  [dim]{index}/{total}[/dim] {name}")

    try:
        summary = enrich_missing_emails(limit=limit, config=config, progress=progress)
    except CRMError as exc:
        _fail(str(exc))

    table = Table(title="補抓信箱結果")
    table.add_column("項目", style="cyan")
    table.add_column("數量", justify="right")
    for label, value in (
        ("檢查的公司", summary.considered),
        ("送出的請求", summary.visited),
        ("找到的信箱", summary.emails_found),
        ("實際更新", summary.updated),
        ("被 robots.txt 擋下", summary.skipped_robots),
        ("沒有可用網址", summary.skipped_no_site),
        ("讀取失敗", summary.failed),
    ):
        table.add_row(label, str(value))
    console.print(table)


@app.command()
def registry(
    limit: Optional[int] = typer.Option(None, "--limit", "-n", help="最多處理幾家公司。"),
) -> None:
    """用統一編號補上經濟部商業司的公司登記資料。

    最有用的是「登記狀態」——名錄不會把倒掉的會員刪掉，這一步把解散、撤銷、
    廢止的挑出來。這一支只吃統一編號（它用的那個資料集不支援名稱查詢）；
    只有公司名稱的名單請改用 complete。
    """
    config = _bootstrap()

    from core.legal import OPEN_DATA_ATTRIBUTION
    from crawler.registry import enrich_registrations

    def progress(index: int, total: int, name: str) -> None:
        console.print(f"  [dim]{index}/{total}[/dim] {name}")

    try:
        summary = enrich_registrations(limit=limit, config=config, progress=progress)
    except CRMError as exc:
        _fail(str(exc))

    table = Table(title="公司登記補完結果")
    table.add_column("項目", style="cyan")
    table.add_column("數量", justify="right")
    for label, value in (
        ("查詢的公司", summary.considered),
        ("查到登記資料", summary.matched),
        ("實際更新", summary.updated),
        ("已停業／解散", summary.defunct),
        ("查無此統編", summary.not_found),
        ("沒有統一編號", summary.skipped_no_tax_id),
        ("對方忙線跳過", summary.busy),
        ("查詢失敗", summary.failed),
    ):
        table.add_row(label, str(value))
    console.print(table)

    if summary.defunct:
        console.print(
            f"[yellow]其中 {summary.defunct} 家的登記狀態已經不是「核准設立」，"
            "寄信之前建議先確認。[/yellow]"
        )
    # 顯名標示是授權條款的強制義務，不是說明文字。見 core.legal。
    console.print(f"[dim]{OPEN_DATA_ATTRIBUTION}[/dim]")


@app.command()
def complete(
    limit: Optional[int] = typer.Option(
        None,
        "--limit",
        "-n",
        help="這次處理幾家。不必指定從第幾家開始——沒跑過的排最前面，跑過的"
        "照上次跑的時間由舊到新，所以再跑一次就是接著上一次跑。",
    ),
    fields: Optional[str] = typer.Option(
        None,
        "--fields",
        "-f",
        help="只補這幾個欄位，逗號分隔。預設全部："
        "tax_id,address,contact_person,website,email,phone,fax",
    ),
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help="連已經有值的欄位也覆蓋。預設只補空的——這個選項會蓋掉你手動填過的資料。",
    ),
) -> None:
    """把只有公司名稱的名單補齊：統編、負責人、地址、官網、信箱、電話、聯絡人。

    跟 enrich、registry 的差別在前提。那兩支各自需要「已經有網址」與「已經
    有統編」；這一支從只有名字開始，一家公司走三關：

      1. 查經濟部商業司（有統編就用統編，只有名稱就用名稱模糊查詢）
      2. 還是沒有網址的話，搜尋找出官網
      3. 到官網上抓公開刊登的信箱、電話、傳真、聯絡人

    搜尋來的網址一定會先驗證（首頁文字要提到這家公司）才採用，通不過就留白。
    """
    config = _bootstrap()

    from core.legal import OPEN_DATA_ATTRIBUTION
    from crawler.complete import FILLABLE_FIELDS, complete_companies

    chosen = None
    if fields:
        chosen = [name.strip() for name in fields.split(",") if name.strip()]
        unknown = set(chosen) - set(FILLABLE_FIELDS)
        if unknown:
            _fail(
                f"不認得的欄位：{'、'.join(sorted(unknown))}。"
                f"可用的有：{', '.join(FILLABLE_FIELDS)}"
            )

    if overwrite:
        console.print(
            "[yellow]--overwrite：已經有值的欄位也會被覆蓋，包含你手動填過的。[/yellow]"
        )

    def progress(index: int, total: int, name: str) -> None:
        console.print(f"  [dim]{index}/{total}[/dim] {name}")

    try:
        summary = complete_companies(
            limit=limit,
            fields=chosen,
            overwrite=overwrite,
            config=config,
            progress=progress,
        )
    except CRMError as exc:
        _fail(str(exc))

    table = Table(title="補齊結果")
    table.add_column("項目", style="cyan")
    table.add_column("數量", justify="right")
    for label, value in (
        ("處理的公司", summary.considered),
        ("實際更新", summary.updated),
        ("補上的欄位總數", summary.fields_filled),
        ("標記為跑過", summary.marked_done),
        ("還剩待補", summary.remaining),
        ("其中還沒跑過", summary.remaining_untried),
        ("商業司對到", summary.registry_matched),
        ("送出的搜尋", summary.searches_made),
        ("找到官網", summary.websites_found),
        ("造訪的頁面", summary.sites_visited),
        ("搜尋到但無法確認", summary.rejected_unconfirmed),
        ("被 robots.txt 擋下", summary.skipped_robots),
        ("商業司忙線跳過", summary.registry_busy),
        ("失敗", summary.failed),
    ):
        table.add_row(label, str(value))
    console.print(table)

    if summary.filled:
        from core.i18n import field_label

        detail = Table(title="各欄位補上幾筆")
        detail.add_column("欄位", style="cyan")
        detail.add_column("數量", justify="right")
        for name, count in sorted(summary.filled.items()):
            detail.add_row(field_label(name), str(count))
        console.print(detail)

    if summary.rejected_unconfirmed:
        console.print(
            f"[dim]{summary.rejected_unconfirmed} 家搜尋到了網頁，但頁面內容沒有提到"
            "該公司，因此沒有採用——寧可留白也不要存錯的網址。[/dim]"
        )
    if summary.search_stopped:
        console.print(f"[yellow]搜尋中途停止：{summary.search_stopped}[/yellow]")
    if summary.search_provider:
        console.print(f"[dim]找官網使用：{summary.search_provider}[/dim]")
    # 顯名標示是授權條款的強制義務，不是說明文字。見 core.legal。
    console.print(f"[dim]{OPEN_DATA_ATTRIBUTION}[/dim]")


@app.command()
def security() -> None:
    """上傳 git 前的安全檢查：找出會外洩機密或個資的檔案。

    只讀不寫。有任何一項不通過就以非零狀態結束，方便掛進 pre-commit hook。
    """
    from core.config import PROJECT_ROOT
    from core.credentials import SecretSource, describe, keyring_available

    problems: list[str] = []
    warnings: list[str] = []

    # --- 1. 憑證存放位置 ---
    cred_table = Table(title="憑證")
    cred_table.add_column("項目", style="cyan")
    cred_table.add_column("存放位置")
    cred_table.add_column("說明", overflow="fold")

    for name, label in (
        ("gmail_address", "Gmail 帳號"),
        ("gmail_app_password", "Gmail 應用程式密碼"),
    ):
        status = describe(name)
        colour = {
            SecretSource.KEYRING: "green",
            SecretSource.ENV: "yellow",
            SecretSource.UNSET: "dim",
        }[status.source]
        cred_table.add_row(label, f"[{colour}]{status.source.value}[/{colour}]", status.hint)
        if status.source is SecretSource.ENV:
            problems.append(f"{label}存在 .env 明文中，請改存到系統憑證保管庫")
    console.print(cred_table)

    if not keyring_available():
        warnings.append("此系統無法使用憑證保管庫，密碼只能存在 .env")

    # --- 2. 資料庫本身的個資保護 ---
    # .gitignore 擋得住「不小心 commit 進去」，擋不住「資料庫檔案被複製走」。
    # 欄位加密處理的是後者，所以它屬於同一份安全檢查。
    from database.encryption import status as encryption_report
    from database.session import get_engine

    db_path = get_config().database.sqlite_path
    crypto_status = None
    if db_path is None or not db_path.exists():
        # 這個指令承諾只讀不寫，連線會順手把空的資料庫檔案建出來。
        warnings.append("資料庫尚未建立，略過加密狀態檢查")
    else:
        try:
            crypto_status = encryption_report(get_engine())
        except CRMError as exc:
            warnings.append(f"無法讀取資料庫加密狀態：{exc}")

    if crypto_status is not None:
        db_table = Table(title="資料庫個資保護")
        db_table.add_column("項目", style="cyan")
        db_table.add_column("狀態", overflow="fold")
        colour = "green" if crypto_status.active else "yellow"
        db_table.add_row("欄位加密", f"[{colour}]{crypto_status.describe()}[/{colour}]")
        db_table.add_row("已加密的欄位值", str(crypto_status.encrypted_values))
        db_table.add_row("明文的欄位值", str(crypto_status.plaintext_values))
        console.print(db_table)

        if not crypto_status.active:
            warnings.append(
                "個資欄位未加密——資料庫檔案若被複製走，信箱與電話可直接讀取"
            )
        elif not crypto_status.fully_converted:
            problems.append(
                f"還有 {crypto_status.pending} 個個資欄位值仍是明文，請重新啟動程式完成轉換"
            )

    # --- 3. .gitignore 覆蓋範圍 ---
    gitignore = PROJECT_ROOT / ".gitignore"
    must_ignore = [
        ".env", "data/", "backups/", "output/", "logs/",
        "custom_sources.yaml", "user_settings.yaml",
    ]
    if not gitignore.exists():
        problems.append("找不到 .gitignore，機密檔案會直接被 commit")
    else:
        content = gitignore.read_text(encoding="utf-8")
        missing = [rule for rule in must_ignore if rule not in content]
        if missing:
            problems.append(f".gitignore 沒有涵蓋：{', '.join(missing)}")

    # --- 4. 實際會被 commit 的檔案 ---
    sensitive_patterns = (
        ".env", ".db", ".sqlite", ".xlsx", ".csv", ".json", ".log", ".pem", ".key",
    )
    tracked = git_tracked_files(PROJECT_ROOT)
    if tracked is None:
        warnings.append("目前不是 git 儲存庫，略過已追蹤檔案檢查")
    else:
        risky = [
            path for path in tracked
            if any(path.endswith(ext) for ext in sensitive_patterns)
            and not path.endswith((".env.example", "requirements.txt"))
            and "templates/mail/範例" not in path
        ]
        if risky:
            problems.append(
                "以下敏感檔案已被 git 追蹤，需先 git rm --cached：\n    "
                + "\n    ".join(risky)
            )

    # --- 4b. 還沒被追蹤、但也沒被忽略的敏感檔 ---
    #
    # 這是風險最高的狀態，而原本的檢查完全看不到它：上面那一段只看「已經被
    # 追蹤的」，下面那張表只看三個寫死的路徑。一份剛匯入用的會員名冊放在專案
    # 根目錄，兩邊都不會提到它——然後 `git add -A` 一下就進了公開的 repo。
    #
    # 實際遇過：`TAMI會員聯絡資訊.xlsx`（2699 家公司的聯絡資料）躺在根目錄，
    # 而這支指令印的是「✓ 檢查通過，可以安全上傳 git」。
    loose = git_untracked_unignored_files(PROJECT_ROOT)
    if loose:
        exposed = [
            path for path in loose
            if any(path.endswith(ext) for ext in sensitive_patterns)
            and not path.endswith((".env.example", "requirements.txt"))
        ]
        if exposed:
            problems.append(
                "以下敏感檔案沒有被 git 追蹤，但也沒有被 .gitignore 忽略——"
                "下一次 git add -A 就會把它們送上去：\n    "
                + "\n    ".join(exposed)
                + "\n  處理方式：把檔案移到專案資料夾外，或加進 .gitignore。"
            )

    # --- 4. 專案資料夾中實際存在的敏感檔 ---
    present = Table(title="專案中的敏感檔案（確認都在 .gitignore 內）")
    present.add_column("檔案", style="cyan", overflow="fold")
    present.add_column("狀態")
    for relative in (".env", "data/crm.db", "custom_sources.yaml"):
        path = PROJECT_ROOT / relative
        if path.exists():
            ignored = gitignore.exists() and any(
                rule.rstrip("/") in relative for rule in must_ignore
            )
            present.add_row(
                relative,
                "[green]已忽略[/green]" if ignored else "[red]未忽略[/red]",
            )
    if present.row_count:
        console.print(present)

    # --- 結論 ---
    for warning in warnings:
        console.print(f"[yellow]提醒：{warning}[/yellow]")

    if problems:
        console.print()
        for problem in problems:
            console.print(f"[bold red]✗[/bold red] {problem}")
        console.print(f"\n[bold red]共 {len(problems)} 項需要處理，先別上傳。[/bold red]")
        raise typer.Exit(code=1)

    console.print("\n[bold green]✓ 檢查通過，可以安全上傳 git。[/bold green]")


@app.command()
def version() -> None:
    """Print the version."""
    console.print(f"{PROJECT_NAME} {VERSION}")


if __name__ == "__main__":
    app()
