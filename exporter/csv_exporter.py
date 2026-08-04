"""CSV export.

Default encoding is ``utf-8-sig``: without the BOM, Excel on a Traditional
Chinese Windows install reads the file as Big5 and mangles every company name.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from core.config import AppConfig
from core.schemas import CompanyView
from exporter.base import BaseExporter


class CsvExporter(BaseExporter):
    extension = "csv"
    label = "CSV (.csv)"

    def __init__(self, config: AppConfig | None = None, encoding: str = "utf-8-sig") -> None:
        super().__init__(config)
        self.encoding = encoding

    def _write(self, frame: pd.DataFrame, target: Path, rows: list[CompanyView]) -> None:
        frame.to_csv(target, index=False, encoding=self.encoding, lineterminator="\n")
