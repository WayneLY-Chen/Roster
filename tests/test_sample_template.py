"""範例檔的價值全在「照著填真的匯得進去」，所以測試就是把它餵回匯入器。"""

from __future__ import annotations

import pytest

from core.errors import ExportError
from exporter.importer import _COLUMN_ALIASES, read_table, rows_to_records
from exporter.sample_template import REQUIRED_FIELD, sample_columns, write_sample


def test_sample_columns_cover_every_field_the_importer_accepts():
    """欄位是推導的，不是手寫的——少一個就代表推導壞了。"""
    expected = dict.fromkeys(_COLUMN_ALIASES.values())
    assert [field for _, field in sample_columns()] == list(expected)


def test_sample_columns_use_chinese_headings():
    headings = dict(sample_columns())
    assert "公司名稱" in headings
    assert headings["公司名稱"] == "company_name"


@pytest.mark.parametrize("suffix", [".csv", ".xlsx"])
def test_generated_sample_imports_cleanly(tmp_path, suffix):
    """真正要保證的事：範例檔本身就是一份合法的匯入檔。"""
    path = write_sample(tmp_path / f"範例{suffix}")
    assert path.exists()

    frame = read_table(path)
    records, unmapped = rows_to_records(frame, "測試")

    assert unmapped == [], f"範例檔有匯入器認不得的欄位：{unmapped}"
    assert len(records) == 3
    assert all(r.company_name for r in records), "每一列都必須有公司名稱"


def test_sample_demonstrates_that_only_the_name_is_required(tmp_path):
    """第三列刻意只填公司名稱，示範其餘欄位可以留白。"""
    path = write_sample(tmp_path / "範例.csv")
    records, _ = rows_to_records(read_table(path), "測試")
    sparse = records[-1]
    assert getattr(sparse, REQUIRED_FIELD)
    assert not sparse.email
    assert not sparse.phone


def test_csv_carries_a_bom_so_excel_does_not_mangle_chinese(tmp_path):
    """沒有 BOM 的話，中文版 Windows 的 Excel 會用 Big5 猜，標題全變亂碼。"""
    path = write_sample(tmp_path / "範例.csv")
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")


def test_unsupported_suffix_is_refused(tmp_path):
    with pytest.raises(ExportError):
        write_sample(tmp_path / "範例.txt")


def test_sample_uses_no_real_addresses(tmp_path):
    """範例信箱一律用 RFC 2606 的保留網域，免得有人直接拿去寄信。"""
    text = write_sample(tmp_path / "範例.csv").read_text(encoding="utf-8-sig")
    for line in text.splitlines()[1:]:
        for cell in line.split(","):
            if "@" in cell:
                assert cell.split("@")[1].startswith("example."), cell
