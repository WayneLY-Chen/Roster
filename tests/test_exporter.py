"""Tests for the exporter package (base shaping, the three writers, the
export facade and the importer)."""

from __future__ import annotations

import json
from datetime import datetime

import openpyxl
import pandas as pd
import pytest

from core.errors import ExportError
from core.schemas import CompanyView
from exporter.base import DEFAULT_COLUMNS, HEADER_LABELS, build_dataframe, resolve_columns
from exporter.csv_exporter import CsvExporter
from exporter.excel import ExcelExporter
from exporter.importer import _canonical, import_file, read_table, rows_to_records
from exporter.json_exporter import JsonExporter
from exporter.service import export_companies


def make_row(**overrides) -> CompanyView:
    defaults = dict(id=1, company_name="測試公司", tags=[], status="Active")
    defaults.update(overrides)
    return CompanyView(**defaults)


# ------------------------------------------------------------- resolve_columns


def test_resolve_columns_filters_unknown_names(tmp_config):
    config = tmp_config.model_copy(
        update={
            "exporter": tmp_config.exporter.model_copy(
                update={"columns": ["company_name", "bogus_field", "email"]}
            )
        }
    )
    assert resolve_columns(config) == ["company_name", "email"]


def test_resolve_columns_falls_back_to_defaults_when_all_unknown(tmp_config):
    config = tmp_config.model_copy(
        update={"exporter": tmp_config.exporter.model_copy(update={"columns": ["bogus"]})}
    )
    assert resolve_columns(config) == list(DEFAULT_COLUMNS)


def test_resolve_columns_uses_defaults_when_unset(tmp_config):
    config = tmp_config.model_copy(
        update={"exporter": tmp_config.exporter.model_copy(update={"columns": []})}
    )
    assert resolve_columns(config) == list(DEFAULT_COLUMNS)


# ------------------------------------------------------------- build_dataframe


def test_build_dataframe_translates_headers(tmp_config):
    row = make_row()
    frame = build_dataframe([row], tmp_config, columns=["company_name"])
    assert list(frame.columns) == [HEADER_LABELS["company_name"]]


def test_build_dataframe_formats_list_as_comma_string(tmp_config):
    row = make_row(tags=["a", "b"])
    frame = build_dataframe([row], tmp_config, columns=["tags"])
    assert frame.iloc[0][HEADER_LABELS["tags"]] == "a, b"


def test_build_dataframe_formats_datetime_with_configured_format(tmp_config):
    row = make_row(created_at=datetime(2026, 1, 2, 3, 4, 5))
    frame = build_dataframe([row], tmp_config, columns=["created_at"])
    assert frame.iloc[0][HEADER_LABELS["created_at"]] == "2026-01-02 03:04:05"


def test_build_dataframe_none_becomes_empty_string(tmp_config):
    row = make_row(email=None)
    frame = build_dataframe([row], tmp_config, columns=["email"])
    assert frame.iloc[0][HEADER_LABELS["email"]] == ""


def test_build_dataframe_formats_a_bare_date_field(tmp_config):
    from datetime import date

    row = make_row(follow_up_date=date(2026, 3, 4))
    frame = build_dataframe([row], tmp_config, columns=["follow_up_date"])
    assert frame.iloc[0][HEADER_LABELS["follow_up_date"]] == "2026-03-04"


def test_build_dataframe_can_skip_header_translation(tmp_config):
    row = make_row()
    frame = build_dataframe([row], tmp_config, columns=["company_name"], translate_headers=False)
    assert list(frame.columns) == ["company_name"]


# ------------------------------------------------------------------ resolve_path


def test_resolve_path_none_returns_timestamped_name_in_output_dir(tmp_config):
    exporter = CsvExporter(tmp_config)
    path = exporter.resolve_path(None)
    assert path.parent == tmp_config.exporter.resolved_output_dir
    assert path.parent.exists()
    assert path.suffix == ".csv"
    assert path.name.startswith("companies-")


def test_resolve_path_directory_gets_a_generated_filename(tmp_config, tmp_path):
    target_dir = tmp_path / "somewhere"
    target_dir.mkdir()
    exporter = CsvExporter(tmp_config)
    path = exporter.resolve_path(target_dir)
    assert path.parent == target_dir
    assert path.suffix == ".csv"


def test_resolve_path_relative_path_is_placed_under_output_dir(tmp_config):
    exporter = CsvExporter(tmp_config)
    path = exporter.resolve_path("subdir/report.csv")
    assert path == tmp_config.exporter.resolved_output_dir / "subdir" / "report.csv"


def test_resolve_path_wrong_suffix_is_corrected(tmp_config, tmp_path):
    exporter = ExcelExporter(tmp_config)
    path = exporter.resolve_path(tmp_path / "report.txt")
    assert path == tmp_path / "report.xlsx"


def test_resolve_path_keeps_correct_suffix(tmp_config, tmp_path):
    exporter = JsonExporter(tmp_config)
    path = exporter.resolve_path(tmp_path / "report.json")
    assert path == tmp_path / "report.json"


# -------------------------------------------------------------------- excel


def test_excel_exporter_writes_a_readable_workbook(tmp_config, tmp_path):
    rows = [make_row(id=1, company_name="Alpha", email="a@example.com")]
    exporter = ExcelExporter(tmp_config)
    path = exporter.export(rows, tmp_path / "companies.xlsx")

    assert path.exists()
    workbook = openpyxl.load_workbook(path)
    sheet = workbook[tmp_config.exporter.excel_sheet_name]
    header_row = [cell.value for cell in sheet[1]]
    assert HEADER_LABELS["company_name"] in header_row
    data_row = [cell.value for cell in sheet[2]]
    assert "Alpha" in data_row


# --------------------------------------------------------------------- csv


def test_csv_exporter_writes_a_readable_csv(tmp_config, tmp_path):
    rows = [make_row(id=1, company_name="Alpha", email="a@example.com")]
    exporter = CsvExporter(tmp_config)
    path = exporter.export(rows, tmp_path / "companies.csv")

    assert path.exists()
    frame = pd.read_csv(path, encoding="utf-8-sig")
    assert frame.loc[0, HEADER_LABELS["company_name"]] == "Alpha"


def test_export_wraps_oserror_from_the_writer(tmp_config, tmp_path, monkeypatch):
    def boom(self, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(pd.DataFrame, "to_csv", boom)
    exporter = CsvExporter(tmp_config)
    with pytest.raises(ExportError):
        exporter.export([make_row(id=1, company_name="Alpha")], tmp_path / "companies.csv")


# -------------------------------------------------------------------- json


def test_json_exporter_writes_metadata_envelope(tmp_config, tmp_path):
    rows = [make_row(id=1, company_name="Alpha", email="a@example.com", tags=["x"])]
    exporter = JsonExporter(tmp_config)
    path = exporter.export(rows, tmp_path / "companies.json")

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["count"] == 1
    assert "exported_at" in payload
    assert "generator" in payload
    assert payload["columns"] == resolve_columns(tmp_config)
    assert payload["companies"][0]["company_name"] == "Alpha"
    assert payload["companies"][0]["tags"] == ["x"]


def test_json_exporter_can_write_unwrapped_array(tmp_config, tmp_path):
    rows = [make_row(id=1, company_name="Alpha")]
    exporter = JsonExporter(tmp_config, wrap=False)
    path = exporter.export(rows, tmp_path / "companies.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, list)
    assert payload[0]["company_name"] == "Alpha"


def test_json_exporter_formats_a_bare_date_field(tmp_config, tmp_path):
    from datetime import date

    config = tmp_config.model_copy(
        update={
            "exporter": tmp_config.exporter.model_copy(
                update={"columns": ["company_name", "follow_up_date"]}
            )
        }
    )
    rows = [make_row(id=1, company_name="Alpha", follow_up_date=date(2026, 3, 4))]
    path = JsonExporter(config).export(rows, tmp_path / "companies.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["companies"][0]["follow_up_date"] == "2026-03-04"


# -------------------------------------------------------------------- service


def test_export_companies_writes_empty_file_when_no_rows_match(db_session, tmp_config):
    path, count = export_companies("csv", path=None, config=tmp_config)
    assert count == 0
    assert path.exists()
    frame = pd.read_csv(path, encoding="utf-8-sig")
    assert len(frame) == 0


def test_export_companies_round_trips_with_repository(db_session, tmp_config):
    from database.repository import CompanyRepository

    repo = CompanyRepository(db_session)
    repo.create(
        company_name="Alpha Metals",
        name_key="alpha",
        dedupe_key="tax:11111111",
        tax_id="11111111",
        email="a@alpha.tw",
    )
    db_session.commit()

    path, count = export_companies("excel", config=tmp_config)
    assert count == 1
    assert path.suffix == ".xlsx"


def test_get_exporter_unknown_format_raises(tmp_config):
    from exporter.service import get_exporter

    with pytest.raises(ExportError):
        get_exporter("word", tmp_config)


def test_get_exporter_accepts_aliases_and_is_case_insensitive(tmp_config):
    from exporter.excel import ExcelExporter
    from exporter.service import get_exporter

    assert isinstance(get_exporter("XLSX", tmp_config), ExcelExporter)
    assert isinstance(get_exporter(".xlsx", tmp_config), ExcelExporter)


def test_available_formats_and_format_labels():
    from exporter.service import FORMAT_LABELS, available_formats

    assert available_formats() == ["excel", "csv", "json"]
    assert set(FORMAT_LABELS) == {"excel", "csv", "json"}


def test_export_all_formats_writes_every_format(db_session, tmp_config):
    from database.repository import CompanyRepository
    from exporter.service import export_all_formats

    CompanyRepository(db_session).create(
        company_name="Alpha Metals", name_key="alpha", dedupe_key="tax:11111111"
    )
    db_session.commit()

    written = export_all_formats(config=tmp_config)
    assert set(written) == {"excel", "csv", "json"}
    for path in written.values():
        assert path.exists()


# ------------------------------------------------------------------ importer


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("company_name", "company_name"),
        ("Company", "company_name"),
        ("公司名稱", "company_name"),
        ("公司名稱 Company", "company_name"),
        ("統一編號", "tax_id"),
        ("統編", "tax_id"),
        ("Email", "email"),
        ("信箱", "email"),
        ("電話 Phone", "phone"),
        ("網址 Website", "website"),
        ("地址 Address", "address"),
        ("產業 Industry", "industry"),
        ("聯絡人 Contact", "contact_person"),
        # 新欄位的雙語標題也要能被匯入器認回來——匯出再匯入是常見的工作
        # 流程，標題漏掉對照的話那幾欄會被當成「認不得的欄位」整個丟掉。
        ("英文名稱 English Name", "english_name"),
        ("英文名稱", "english_name"),
        ("傳真 Fax", "fax"),
        ("傳真", "fax"),
        ("主要產品 Products", "products"),
        ("營業項目", "products"),
        ("備註 Remark", "remark"),
        ("Totally Unknown Column", None),
        ("", None),
    ],
)
def test_canonical_header_resolution(header, expected):
    assert _canonical(header) == expected


def test_read_table_csv(tmp_path):
    frame = pd.DataFrame({"company_name": ["Foo"], "email": ["foo@example.com"]})
    path = tmp_path / "in.csv"
    frame.to_csv(path, index=False, encoding="utf-8-sig")
    loaded = read_table(path)
    assert loaded.loc[0, "company_name"] == "Foo"


def test_read_table_excel(tmp_path):
    frame = pd.DataFrame({"company_name": ["Foo"], "email": ["foo@example.com"]})
    path = tmp_path / "in.xlsx"
    frame.to_excel(path, index=False)
    loaded = read_table(path)
    assert loaded.loc[0, "company_name"] == "Foo"


def test_read_table_unwraps_this_apps_own_json_export(tmp_config, tmp_path):
    """The app must be able to re-import the JSON it exports.

    :class:`JsonExporter` wraps the records in a metadata envelope
    (``exported_at``/``generator``/``count``/``columns``/``companies``).
    ``read_table`` has to unwrap that and return the company records --
    a single exported row is the realistic case and must work.
    """
    rows = [make_row(id=1, company_name="Alpha", email="a@example.com")]
    JsonExporter(tmp_config).export(rows, tmp_path / "out.json")

    loaded = read_table(tmp_path / "out.json")

    assert "company_name" in loaded.columns
    assert "companies" not in loaded.columns
    assert len(loaded) == 1
    assert loaded.loc[0, "company_name"] == "Alpha"


def test_read_table_unwraps_json_export_with_several_rows(tmp_config, tmp_path):
    """Unwrapping must not depend on how row count compares to column count."""
    config = tmp_config.model_copy(
        update={
            "exporter": tmp_config.exporter.model_copy(
                update={"columns": ["company_name", "email"]}
            )
        }
    )
    rows = [
        make_row(id=1, company_name="Alpha", email="a@example.com"),
        make_row(id=2, company_name="Beta", email="b@example.com"),
    ]
    JsonExporter(config).export(rows, tmp_path / "out.json")

    loaded = read_table(tmp_path / "out.json")

    assert list(loaded.columns) == ["company_name", "email"]
    assert sorted(loaded["company_name"]) == ["Alpha", "Beta"]


def test_read_table_accepts_a_bare_json_array(tmp_path):
    """A plain array of objects (not from this app) is also valid input."""
    path = tmp_path / "plain.json"
    path.write_text(
        '[{"company_name": "甲公司", "email": "a@b.tw"}]', encoding="utf-8"
    )

    loaded = read_table(path)

    assert loaded.loc[0, "company_name"] == "甲公司"


def test_read_table_rejects_unreadable_json_structure(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"companies": "not-a-list"}', encoding="utf-8")

    with pytest.raises(ExportError):
        read_table(path)


def test_read_table_missing_file_raises(tmp_path):
    with pytest.raises(ExportError):
        read_table(tmp_path / "does-not-exist.csv")


def test_read_table_csv_falls_back_through_encodings_to_big5(tmp_path):
    path = tmp_path / "in.csv"
    path.write_bytes("公司名稱,email\n測試,a@b.com\n".encode("big5"))
    frame = read_table(path)
    assert frame.loc[0, "公司名稱"] == "測試"


def test_read_table_csv_undecodable_in_every_encoding_raises(tmp_path):
    path = tmp_path / "in.csv"
    path.write_bytes(b"\x80\x81\xff\xfe")
    with pytest.raises(ExportError):
        read_table(path)


def test_read_table_unsupported_extension_raises(tmp_path):
    path = tmp_path / "in.docx"
    path.write_text("hello")
    with pytest.raises(ExportError):
        read_table(path)


def test_rows_to_records_maps_columns_and_collects_remark():
    frame = pd.DataFrame(
        {
            "公司名稱": ["Foo Inc", ""],
            "統一編號": ["22099131", None],
            "Email": ["foo@example.com", None],
            "備註": ["important", None],
        }
    )
    records, unmapped = rows_to_records(frame, "test-import")
    assert unmapped == []
    assert len(records) == 1  # the blank-name row is dropped
    record = records[0]
    assert record.company_name == "Foo Inc"
    assert record.tax_id == "22099131"
    assert record.email == "foo@example.com"
    assert record.extra["remark"] == "important"
    assert record.source == "test-import"


def test_rows_to_records_no_company_name_column_raises():
    frame = pd.DataFrame({"email": ["a@example.com"]})
    with pytest.raises(ExportError):
        rows_to_records(frame, "test-import")


def test_import_file_round_trip_merges_existing_records(db_session, tmp_config, tmp_path):
    from database.repository import CompanyRepository

    repo = CompanyRepository(db_session)
    repo.create(
        company_name="Alpha Metals",
        name_key="alpha",
        dedupe_key="tax:11111111",
        tax_id="11111111",
        email="a@alpha.tw",
    )
    db_session.commit()

    rows = repo.search_views()
    export_path = tmp_path / "export.csv"
    CsvExporter(tmp_config).export(rows, export_path)

    summary = import_file(export_path, source_label="reimport", config=tmp_config)

    assert summary.rows_read == 1
    assert summary.records_new == 0
    assert summary.records_merged == 1
    assert summary.records_stored == 1
    assert repo.count() == 1  # no duplicate row was created


def test_import_file_creates_new_records(db_session, tmp_config, tmp_path):
    from database.repository import CompanyRepository

    path = tmp_path / "new.csv"
    path.write_text(
        "公司名稱,Email\n全新公司,new@example.com\n", encoding="utf-8-sig"
    )

    summary = import_file(path, source_label="reimport", config=tmp_config)

    assert summary.records_new == 1
    assert summary.records_merged == 0
    assert CompanyRepository(db_session).count() == 1


# ---------------------------------------------- 匯入時認出公司名稱那一欄
#
# 使用者實際踩到的：匯入一份政府網站下載的名冊，得到「no company-name column
# found. Expected one of: company_name, company, name, 公司名稱, 廠商名稱」，
# 而他的檔案裡明明就有公司名字那一欄，只是標題叫別的。


@pytest.mark.parametrize(
    "header",
    [
        "工廠名稱",          # 經濟部工廠登記查詢
        "廠商全名(中文)",     # 公協會名冊很常見的寫法
        "事業單位名稱",
        "企業名稱",
        "會員公司",
        "Company Name",
        "名稱",
    ],
)
def test_a_company_name_column_is_recognised_however_it_is_labelled(header):
    """標題列不完，所以精確比對之外一定要有「看起來像不像」那一層。"""
    from exporter.importer import rows_to_records

    frame = pd.DataFrame({header: ["台灣積體電路製造股份有限公司"]})
    records, _ = rows_to_records(frame, "test")

    assert [r.company_name for r in records] == ["台灣積體電路製造股份有限公司"]


@pytest.mark.parametrize("header", ["負責人姓名", "聯絡人名稱", "英文名稱"])
def test_a_person_column_is_never_mistaken_for_the_company_name(header):
    """猜錯比認不出來糟得多——它不會報錯，只會讓整份名單的公司名變成人名。"""
    from core.errors import ExportError
    from exporter.importer import rows_to_records

    frame = pd.DataFrame({header: ["王小明"]})
    with pytest.raises(ExportError):
        rows_to_records(frame, "test")


def test_the_error_says_what_columns_the_file_actually_has():
    """讀的人要知道該去改哪一欄，光講我們期待什麼沒有用。

    原本的訊息只列出五個接受的名稱。使用者的標題常常長得像「廠商全名(中文)」，
    看起來明明就對，卻不知道為什麼不行。
    """
    from core.errors import ExportError
    from exporter.importer import rows_to_records

    frame = pd.DataFrame({"編號": ["1"], "統一編號": ["22099131"]})
    with pytest.raises(ExportError) as caught:
        rows_to_records(frame, "test")

    message = str(caught.value)
    assert "編號" in message and "統一編號" in message   # 檔案裡實際有的欄位
    assert "公司名稱" in message                          # 該改成什麼
