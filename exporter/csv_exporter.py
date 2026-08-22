"""CSV export.

Default encoding is ``utf-8-sig``: without the BOM, Excel on a Traditional
Chinese Windows install reads the file as Big5 and mangles every company name.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from core.config import AppConfig
from core.schemas import CompanyView
from exporter.base import BaseExporter, registry_attribution


class CsvExporter(BaseExporter):
    extension = "csv"
    label = "CSV (.csv)"
    #: 會被雙擊丟進 Excel，必須中和公式。
    spreadsheet_safe = True

    def __init__(self, config: AppConfig | None = None, encoding: str = "utf-8-sig") -> None:
        super().__init__(config)
        self.encoding = encoding

    def _write(self, frame: pd.DataFrame, target: Path, rows: list[CompanyView]) -> None:
        attribution = registry_attribution(rows)
        if attribution:
            # 補成一整列，第一格寫來源、其餘留空。
            #
            # 不寫成 ``# ...`` 開頭的註解行：CSV 沒有註解語法，那樣寫會讓
            # 嚴格一點的讀取器（pandas 的 C 引擎、各種 ETL 工具）直接在最後
            # 一行報錯。補成合法的一列，檔案仍然是方的，Excel 打開就是最下面
            # 多一行字。這一行不能省，見 core.legal 的說明。
            padding = [""] * (len(frame.columns) - 1)
            frame = pd.concat(
                [frame, pd.DataFrame([[attribution, *padding]], columns=frame.columns)],
                ignore_index=True,
            )
        frame.to_csv(target, index=False, encoding=self.encoding, lineterminator="\n")
