"""JSON export.

Unlike the tabular formats this keeps machine-friendly field names and native
types (tags stay a list, dates stay ISO-8601), because JSON output is consumed
by other programs rather than read in a spreadsheet.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from core.constants import PROJECT_NAME, VERSION
from core.schemas import CompanyView
from exporter.base import BaseExporter, registry_attribution, resolve_columns


class JsonExporter(BaseExporter):
    extension = "json"
    label = "JSON (.json)"

    def __init__(self, config=None, indent: int = 2, wrap: bool = True) -> None:
        super().__init__(config)
        self.indent = indent
        #: When True the array is wrapped in an object carrying export metadata.
        self.wrap = wrap

    def _write(self, frame: pd.DataFrame, target: Path, rows: list[CompanyView]) -> None:
        columns = resolve_columns(self.config)
        records = [
            {column: _jsonable(getattr(row, column, None)) for column in columns}
            for row in rows
        ]
        payload: object = records
        if self.wrap:
            payload = {
                "exported_at": datetime.now().isoformat(timespec="seconds"),
                "generator": f"{PROJECT_NAME} {VERSION}",
                "count": len(records),
                "columns": columns,
                "companies": records,
            }
            # 開放資料的顯名標示（見 core.legal）。只有這批資料裡真的有公司
            # 登記資料時才會出現。``wrap=False`` 的裸陣列沒有地方可以放它，
            # 所以要匯出含登記資料的 JSON 時不要關掉外層物件。
            attribution = registry_attribution(rows)
            if attribution:
                payload["attribution"] = attribution  # type: ignore[index]
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=self.indent),
            encoding="utf-8",
        )


def _jsonable(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    return value
