"""自由欄位（``extra_fields``）從爬取到匯出的整條路徑。

每個名錄列的欄位都不一樣——旅行公會有「會員代表」「入會年月日」，化工公會
有「代理廠商及代銷產品」。固定欄位裝不下它們，丟掉又等於使用者在網頁上看得
到、在程式裡卻找不到。這個檔案釘住「抓到什麼就留什麼」這條路。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from core.schemas import CompanyView, RawCompany  # noqa: E402
from database.repository import CompanyRepository  # noqa: E402
from exporter.base import build_dataframe, extra_columns  # noqa: E402
from exporter.importer import rows_to_records  # noqa: E402
from verifier.service import CleaningService  # noqa: E402

MEMBER = "會員代表"
JOINED = "入會年月日"


# ------------------------------------------------------------ 清理階段


def test_cleaning_carries_free_form_fields_through(tmp_config):
    raw = RawCompany(
        company_name="東晟旅行社股份有限公司",
        extra_fields={MEMBER: "陳萬中", JOINED: "1970 年 10 月 31 日"},
    )
    clean = CleaningService(tmp_config).clean(raw)
    assert clean is not None
    assert clean.extra_fields[MEMBER] == "陳萬中"


def test_blank_names_and_values_are_dropped(tmp_config):
    raw = RawCompany(
        company_name="測試公司",
        extra_fields={"  ": "沒有名稱", MEMBER: "   ", JOINED: " 1970 "},
    )
    clean = CleaningService(tmp_config).clean(raw)
    assert clean is not None
    assert clean.extra_fields == {JOINED: "1970"}


def test_a_runaway_value_is_truncated_rather_than_stored_whole(tmp_config):
    """名錄偶爾把整段公告寫成「標題：內文」。截斷比讓詳細資料視窗塞不下好。"""
    raw = RawCompany(company_name="測試公司", extra_fields={"公告": "字" * 5000})
    clean = CleaningService(tmp_config).clean(raw)
    assert clean is not None
    assert len(clean.extra_fields["公告"]) == 500


# ------------------------------------------------------------ 資料庫


def _clean(config, name: str, **extra: str):
    return CleaningService(config).clean(
        RawCompany(company_name=name, extra_fields=extra)
    )


def test_free_form_fields_survive_a_write_and_read(db_session, tmp_config):
    repo = CompanyRepository(db_session)
    record = _clean(tmp_config, "旅行社甲", **{MEMBER: "陳萬中"})
    company, _ = repo.upsert(record)
    db_session.commit()

    reloaded = repo.get(company.id)
    assert reloaded is not None
    assert reloaded.extra_fields[MEMBER] == "陳萬中"


def test_an_upsert_fills_gaps_but_never_overwrites(db_session, tmp_config):
    """跟固定欄位同一條規則：爬取補空白，不覆蓋使用者已經整理過的內容。"""
    repo = CompanyRepository(db_session)
    company, _ = repo.upsert(_clean(tmp_config, "旅行社乙", **{MEMBER: "原本的人"}))
    db_session.commit()

    repo.upsert(_clean(tmp_config, "旅行社乙", **{MEMBER: "新抓到的人", JOINED: "1970"}))
    db_session.commit()

    reloaded = repo.get(company.id)
    assert reloaded is not None
    assert reloaded.extra_fields[MEMBER] == "原本的人"
    assert reloaded.extra_fields[JOINED] == "1970"


def test_merging_two_records_keeps_both_sets_of_free_form_fields(db_session, tmp_config):
    repo = CompanyRepository(db_session)
    keeper, _ = repo.upsert(_clean(tmp_config, "旅行社丙", **{MEMBER: "陳萬中"}))
    victim, _ = repo.upsert(_clean(tmp_config, "旅行社丁", **{JOINED: "1970"}))
    db_session.commit()

    merged = repo.merge(keeper.id, [victim.id])
    db_session.commit()

    assert merged.extra_fields[MEMBER] == "陳萬中"
    assert merged.extra_fields[JOINED] == "1970"


def test_the_view_exposes_them_so_the_gui_can_show_them(db_session, tmp_config):
    repo = CompanyRepository(db_session)
    company, _ = repo.upsert(_clean(tmp_config, "旅行社戊", **{MEMBER: "陳萬中"}))
    db_session.commit()

    assert CompanyRepository.to_view(company).extra_fields[MEMBER] == "陳萬中"


# ------------------------------------------------------------ 匯出


def _view(company_id: int, name: str, **extra: str) -> CompanyView:
    return CompanyView(id=company_id, company_name=name, extra_fields=extra)


def test_export_appends_one_column_per_free_form_field(tmp_config):
    rows = [
        _view(1, "旅行社甲", **{MEMBER: "陳萬中", JOINED: "1970"}),
        _view(2, "旅行社乙", **{MEMBER: "林伯洲"}),
    ]
    frame = build_dataframe(rows, tmp_config, columns=["company_name"])

    assert list(frame.columns) == ["公司名稱 Company", MEMBER, JOINED]
    assert frame[MEMBER].tolist() == ["陳萬中", "林伯洲"]
    # 沒有這一項的公司留空白，不是 NaN——匯出檔要能直接給人看。
    assert frame[JOINED].tolist() == ["1970", ""]


def test_the_more_common_field_comes_first(tmp_config):
    rows = [
        _view(1, "甲", **{MEMBER: "a"}),
        _view(2, "乙", **{MEMBER: "b"}),
        _view(3, "丙", **{JOINED: "c"}),
    ]
    assert extra_columns(rows) == [MEMBER, JOINED]


def test_the_number_of_extra_columns_is_capped(tmp_config):
    rows = [_view(1, "甲", **{f"欄位{i}": str(i) for i in range(50)})]
    assert len(extra_columns(rows)) == 20


def test_the_raw_dictionary_never_becomes_a_column(tmp_config):
    """整包字典塞進一格對誰都沒有用，它會被攤成一欄一個欄位。"""
    rows = [_view(1, "甲", **{MEMBER: "陳萬中"})]
    frame = build_dataframe(
        rows, tmp_config, columns=["company_name", "extra_fields"], translate_headers=False
    )
    assert "extra_fields" not in frame.columns
    assert MEMBER in frame.columns


# ------------------------------------------------------------ 匯入


def test_columns_with_no_home_are_kept_instead_of_discarded():
    """匯出時每個自由欄位各佔一欄，不收回來的話「匯出→在 Excel 改→匯入」
    這一趟會把它們全部洗掉。"""
    frame = pd.DataFrame(
        [{"公司名稱": "旅行社甲", MEMBER: "陳萬中", JOINED: "1970"}], dtype=object
    )
    records, unmapped = rows_to_records(frame, "test")

    assert records[0].extra_fields == {MEMBER: "陳萬中", JOINED: "1970"}
    # 仍然照實回報，讓使用者知道這些欄位沒有對應到固定欄位。
    assert set(unmapped) == {MEMBER, JOINED}


def test_a_placeholder_header_is_not_a_field():
    """pandas 給沒有標題的欄位取名 "Unnamed: 1"，那是版面不是資料。"""
    frame = pd.DataFrame([{"公司名稱": "旅行社甲", "Unnamed: 1": "x"}], dtype=object)
    records, _unmapped = rows_to_records(frame, "test")
    assert records[0].extra_fields == {}


def test_an_empty_cell_does_not_create_an_empty_field():
    frame = pd.DataFrame(
        [{"公司名稱": "旅行社甲", MEMBER: "陳萬中"}, {"公司名稱": "旅行社乙", MEMBER: None}],
        dtype=object,
    )
    records, _unmapped = rows_to_records(frame, "test")
    assert records[0].extra_fields == {MEMBER: "陳萬中"}
    assert records[1].extra_fields == {}


# ------------------------------------------------------------ 介面


@pytest.fixture(scope="module")
def qt_app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_the_detail_dialog_shows_and_saves_them(qt_app, db_session, tmp_config):
    from controllers.core import CompanyController
    from gui_qt.company_detail import CompanyDetailDialog

    repo = CompanyRepository(db_session)
    company, _ = repo.upsert(_clean(tmp_config, "旅行社己", **{MEMBER: "陳萬中"}))
    db_session.commit()

    dialog = CompanyDetailDialog(None, CompanyController(), company.id, on_saved=None)
    try:
        assert dialog.extra_table.rowCount() == 1
        assert dialog.extra_table.item(0, 0).text() == MEMBER
        assert dialog.extra_table.item(0, 1).text() == "陳萬中"

        dialog.extra_table.item(0, 1).setText("林伯洲")
        dialog._append_extra_row(JOINED, "1970")
        dialog._save()
    finally:
        dialog.close()

    db_session.expire_all()
    reloaded = repo.get(company.id)
    assert reloaded is not None
    assert reloaded.extra_fields == {MEMBER: "林伯洲", JOINED: "1970"}


def test_a_row_with_no_field_name_is_dropped_on_save(qt_app, db_session, tmp_config):
    """使用者按了「新增欄位」卻沒填名稱，不該存下一個沒有名字的欄位。"""
    from controllers.core import CompanyController
    from gui_qt.company_detail import CompanyDetailDialog

    repo = CompanyRepository(db_session)
    company, _ = repo.upsert(_clean(tmp_config, "旅行社庚"))
    db_session.commit()

    dialog = CompanyDetailDialog(None, CompanyController(), company.id, on_saved=None)
    try:
        dialog._append_extra_row("", "沒有名稱的值")
        assert dialog._collect_extra_fields() == {}
    finally:
        dialog.close()
