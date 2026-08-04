"""Export facade.

The CLI and GUI call :func:`export_companies` with a format name and a filter;
everything else (opening a session, running the query, choosing a filename) is
handled here so neither caller has to know about repositories.
"""

from __future__ import annotations

from pathlib import Path

from core.config import AppConfig, get_config
from core.constants import LogCategory
from core.errors import ExportError
from core.logging_setup import get_logger
from core.schemas import CompanyFilter, CompanyView
from database.repository import CompanyRepository
from database.session import session_scope
from exporter.base import BaseExporter
from exporter.csv_exporter import CsvExporter
from exporter.excel import ExcelExporter
from exporter.json_exporter import JsonExporter

log = get_logger(LogCategory.EXPORT)

EXPORTERS: dict[str, type[BaseExporter]] = {
    "excel": ExcelExporter,
    "xlsx": ExcelExporter,
    "csv": CsvExporter,
    "json": JsonExporter,
}

FORMAT_LABELS = {
    "excel": ExcelExporter.label,
    "csv": CsvExporter.label,
    "json": JsonExporter.label,
}


def available_formats() -> list[str]:
    """Canonical format names, for CLI help and GUI dropdowns."""
    return ["excel", "csv", "json"]


def get_exporter(format_name: str, config: AppConfig | None = None) -> BaseExporter:
    exporter_class = EXPORTERS.get(format_name.strip().lower().lstrip("."))
    if exporter_class is None:
        raise ExportError(
            f"unknown export format {format_name!r}; "
            f"available: {', '.join(available_formats())}"
        )
    return exporter_class(config or get_config())


def fetch_rows(
    criteria: CompanyFilter | None = None, config: AppConfig | None = None
) -> list[CompanyView]:
    """Run the query behind an export, outside any export-specific code."""
    with session_scope() as session:
        return CompanyRepository(session).search_views(criteria)


def export_companies(
    format_name: str = "excel",
    path: str | Path | None = None,
    criteria: CompanyFilter | None = None,
    config: AppConfig | None = None,
    columns: list[str] | None = None,
) -> tuple[Path, int]:
    """Export matching companies. Returns ``(written_path, row_count)``.

    Exporting an empty result set still writes a headers-only file -- a
    scheduled export that silently produces nothing is worse than one that
    produces a visibly empty sheet.
    """
    config = config or get_config()
    exporter = get_exporter(format_name, config)
    rows = fetch_rows(criteria, config)

    if not rows:
        log.warning("export matched no companies; writing an empty file")

    written = exporter.export(rows, path, columns)
    return written, len(rows)


def export_all_formats(
    criteria: CompanyFilter | None = None,
    config: AppConfig | None = None,
) -> dict[str, Path]:
    """Write the same result set to every supported format."""
    config = config or get_config()
    rows = fetch_rows(criteria, config)
    written: dict[str, Path] = {}
    for name in available_formats():
        written[name] = get_exporter(name, config).export(rows)
    return written
