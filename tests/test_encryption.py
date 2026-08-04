"""個資欄位加密的測試。

這裡的重點不是「加解密函式對不對」——那是 ``test_crypto.py`` 的事——而是**加密
打開之後，整個資料層還能不能照常運作**。加密是確定性的，所以等值查詢理應不受
影響；受影響的是排序與模糊比對，那些改在 Python 端做。兩者都必須被實際驗證，
因為它們一旦壞掉，症狀是「爬蟲把同一家公司重複寫了兩百筆」這種很晚才會被發現
的問題。

每個測試都用 :func:`tests.conftest.fake_vault` 提供一把全新的金鑰，所以測試之間
不會共用密文，也不會碰到使用者本機真正的憑證保管庫。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from core import crypto
from core.constants import EmailVerdict, RecordStatus
from core.errors import CRMError
from core.schemas import CleanCompany, CompanyFilter
from database import encryption
from database.models import Activity, Company, Contact, EmailMessage
from database.repository import (
    ActivityRepository,
    CompanyRepository,
    ContactRepository,
)
from database.types import (
    EncryptedString,
    email_equals,
    encryption_active,
    is_encrypted_column,
)


def _company(name: str, **overrides) -> CleanCompany:
    fields = {
        "company_name": name,
        "name_key": name,
        "dedupe_key": f"n:{name}",
        "email": None,
        "phone": None,
        "source": "unit-test",
        "email_verdict": EmailVerdict.UNKNOWN,
        "status": RecordStatus.ACTIVE,
    }
    fields.update(overrides)
    return CleanCompany(**fields)


def _raw(session, table: str, column: str) -> list[str | None]:
    """欄位在磁碟上真正的樣子，繞過 TypeDecorator。"""
    return [row[0] for row in session.execute(text(f'SELECT "{column}" FROM "{table}"'))]


def _raise(exc: Exception):
    """做成 monkeypatch 用的替身：呼叫就丟出這個例外。"""

    def _boom(*args, **kwargs):
        raise exc

    return _boom


# ------------------------------------------------------------------ 欄位選擇


def test_personal_data_columns_are_encrypted() -> None:
    assert is_encrypted_column(Company.email)
    assert is_encrypted_column(Company.phone)
    assert is_encrypted_column(Company.address)
    assert is_encrypted_column(Company.contact_person)
    assert is_encrypted_column(Company.remark)
    assert is_encrypted_column(Company.dedupe_key)
    assert is_encrypted_column(Contact.name)
    assert is_encrypted_column(Contact.email)
    assert is_encrypted_column(Activity.body)
    assert is_encrypted_column(EmailMessage.to_address)


def test_business_identifiers_stay_in_clear() -> None:
    """公司名稱與統編留明文，SQL 才能搜尋與排序。"""
    assert not is_encrypted_column(Company.company_name)
    assert not is_encrypted_column(Company.name_key)
    assert not is_encrypted_column(Company.tax_id)
    assert not is_encrypted_column(Company.industry)
    assert not is_encrypted_column(Company.website)
    assert not is_encrypted_column(Company.source)


def test_encrypted_columns_are_derived_from_the_models() -> None:
    columns = encryption.encrypted_columns()
    assert "email" in columns["companies"]
    assert "company_name" not in columns["companies"]
    assert "tags" not in columns          # 標籤沒有個資，整張表不該出現


def test_declared_length_covers_the_ciphertext() -> None:
    """欄位宣告的長度必須裝得下密文，中文也一樣。"""
    for plaintext in ("a" * 64, "台北市信義區松高路一號九樓之三" * 4):
        assert crypto.ciphertext_length(len(plaintext)) >= len(
            crypto.PREFIX
        ) + len(plaintext)

    column = EncryptedString(64)
    assert column.impl_instance.length == crypto.ciphertext_length(64)


# ---------------------------------------------------------------- 實際加密


def test_values_are_ciphertext_on_disk(encryption_on, db_session) -> None:
    repo = CompanyRepository(db_session)
    repo.upsert(
        _company("測試公司", email="ceo@example.com", phone="0227231234",
                 address="台北市信義區松高路1號", contact_person="王小明")
    )
    db_session.commit()

    for column in ("email", "phone", "address", "contact_person", "dedupe_key"):
        stored = _raw(db_session, "companies", column)[0]
        assert stored.startswith(crypto.PREFIX), f"{column} 沒有被加密"

    # 明文不該以任何形式留在那一列裡。
    row = db_session.execute(text("SELECT * FROM companies")).first()
    blob = "".join(str(value) for value in row)
    assert "ceo@example.com" not in blob
    assert "王小明" not in blob
    assert "松高路" not in blob


def test_company_name_is_still_readable_on_disk(encryption_on, db_session) -> None:
    """商業資訊維持明文——這是刻意的，不是漏掉的。"""
    CompanyRepository(db_session).upsert(_company("測試公司"))
    db_session.commit()
    assert _raw(db_session, "companies", "company_name") == ["測試公司"]


def test_orm_reads_back_plaintext(encryption_on, db_session) -> None:
    repo = CompanyRepository(db_session)
    repo.upsert(_company("測試公司", email="ceo@example.com", address="台北市中山區"))
    db_session.commit()
    db_session.expire_all()

    company = repo.all()[0]
    assert company.email == "ceo@example.com"
    assert company.address == "台北市中山區"


def test_disabled_encryption_writes_plaintext(patch_config, db_session) -> None:
    """沒有保管庫（測試預設）時退回明文，而不是寫入壞掉的值。"""
    assert not encryption_active()
    CompanyRepository(db_session).upsert(_company("測試公司", email="a@example.com"))
    db_session.commit()
    assert _raw(db_session, "companies", "email") == ["a@example.com"]


# ------------------------------------------------------------ 等值查詢仍可用


def test_lookup_by_email_finds_an_encrypted_row(encryption_on, db_session) -> None:
    repo = CompanyRepository(db_session)
    repo.upsert(_company("測試公司", email="ceo@example.com"))
    db_session.commit()

    found = repo.get_by_email("ceo@example.com")
    assert found is not None and found.company_name == "測試公司"


def test_lookup_by_email_ignores_case(encryption_on, db_session) -> None:
    """密文不能 lower()，所以大小寫要在加密前就統一。"""
    repo = CompanyRepository(db_session)
    repo.upsert(_company("測試公司", email="CEO@Example.COM"))
    db_session.commit()

    assert repo.get_by_email("ceo@example.com") is not None
    assert repo.get_by_email("CEO@EXAMPLE.COM") is not None


def test_lookup_by_dedupe_key_works(encryption_on, db_session) -> None:
    repo = CompanyRepository(db_session)
    repo.upsert(_company("測試公司", dedupe_key="mail:ceo@example.com"))
    db_session.commit()
    assert repo.get_by_dedupe_key("mail:ceo@example.com") is not None


def test_reinserting_the_same_company_merges_rather_than_duplicates(
    encryption_on, db_session
) -> None:
    """加密最容易弄壞的就是這件事：查不到既有資料，就會一直新增。"""
    repo = CompanyRepository(db_session)
    repo.upsert(_company("測試公司", dedupe_key="mail:ceo@example.com",
                         email="ceo@example.com"))
    db_session.commit()

    _, merged = repo.upsert(
        _company("測試公司", dedupe_key="mail:ceo@example.com",
                 email="ceo@example.com", phone="0227231234")
    )
    db_session.commit()

    assert merged is True
    assert len(repo.all()) == 1
    assert repo.all()[0].phone == "0227231234"


def test_match_by_name_and_phone_survives_encryption(encryption_on, db_session) -> None:
    """name_key 明文、phone 密文的複合條件也要照樣命中。"""
    repo = CompanyRepository(db_session)
    repo.upsert(_company("測試公司", phone="0227231234"))
    db_session.commit()

    match = repo.find_match(
        _company("測試公司", dedupe_key="n:別的鍵", phone="0227231234")
    )
    assert match is not None


def test_has_email_filter_still_works_in_sql(encryption_on, db_session) -> None:
    repo = CompanyRepository(db_session)
    repo.upsert(_company("有信箱", dedupe_key="n:有信箱", email="a@example.com"))
    repo.upsert(_company("沒信箱", dedupe_key="n:沒信箱"))
    db_session.commit()

    with_email = repo.search(CompanyFilter(has_email=True))
    without = repo.search(CompanyFilter(has_email=False))
    assert [c.company_name for c in with_email] == ["有信箱"]
    assert [c.company_name for c in without] == ["沒信箱"]


def test_distinct_values_returns_plaintext(encryption_on, db_session) -> None:
    repo = CompanyRepository(db_session)
    repo.upsert(_company("甲", dedupe_key="n:甲", phone="0227231234"))
    repo.upsert(_company("乙", dedupe_key="n:乙", phone="0227231234"))
    repo.upsert(_company("丙", dedupe_key="n:丙", phone="0287654321"))
    db_session.commit()

    assert repo.distinct_values("phone") == ["0227231234", "0287654321"]


# ------------------------------------------------------- 搜尋與排序（Python 端）


@pytest.fixture
def three_companies(encryption_on, db_session):
    repo = CompanyRepository(db_session)
    repo.upsert(_company("宏達電子", dedupe_key="n:宏達電子",
                         email="zoe@htc-example.com", phone="0227231234"))
    repo.upsert(_company("聯發科技", dedupe_key="n:聯發科技",
                         email="alan@mtk-example.com", phone="0355667788"))
    repo.upsert(_company("台積電子", dedupe_key="n:台積電子",
                         email="mary@tsmc-example.com", phone="0366778899"))
    db_session.commit()
    return repo


def test_free_text_search_matches_an_encrypted_column(three_companies) -> None:
    found = three_companies.search(CompanyFilter(text="mtk-example"))
    assert [c.company_name for c in found] == ["聯發科技"]


def test_free_text_search_still_matches_a_plaintext_column(three_companies) -> None:
    found = three_companies.search(CompanyFilter(text="台積"))
    assert [c.company_name for c in found] == ["台積電子"]


def test_field_filter_matches_inside_an_encrypted_value(three_companies) -> None:
    found = three_companies.search(CompanyFilter(email="alan@"))
    assert [c.company_name for c in found] == ["聯發科技"]


def test_search_by_email_is_case_insensitive(three_companies) -> None:
    assert len(three_companies.search(CompanyFilter(text="MTK-EXAMPLE"))) == 1


def test_sorting_by_an_encrypted_column_uses_plaintext_order(three_companies) -> None:
    """密文的字典序與明文無關，排出來必須是 alan < mary < zoe。"""
    ordered = three_companies.search(
        CompanyFilter(order_by="email", descending=False)
    )
    assert [c.email for c in ordered] == [
        "alan@mtk-example.com",
        "mary@tsmc-example.com",
        "zoe@htc-example.com",
    ]


def test_descending_sort_reverses_the_same_order(three_companies) -> None:
    ordered = three_companies.search(CompanyFilter(order_by="email", descending=True))
    assert [c.email for c in ordered][0] == "zoe@htc-example.com"


def test_rows_without_a_value_sort_last(encryption_on, db_session) -> None:
    repo = CompanyRepository(db_session)
    repo.upsert(_company("有", dedupe_key="n:有", email="a@example.com"))
    repo.upsert(_company("無", dedupe_key="n:無"))
    db_session.commit()

    ordered = repo.search(CompanyFilter(order_by="email", descending=False))
    assert [c.company_name for c in ordered] == ["有", "無"]


def test_paging_applies_after_the_python_filter(three_companies) -> None:
    """LIMIT 交給 SQL 會切錯集合——分頁必須在 Python 過濾之後。"""
    page = three_companies.search(
        CompanyFilter(text="example.com", order_by="email", descending=False, limit=2)
    )
    assert [c.email for c in page] == [
        "alan@mtk-example.com",
        "mary@tsmc-example.com",
    ]

    second = three_companies.search(
        CompanyFilter(
            text="example.com", order_by="email", descending=False, limit=2, offset=2
        )
    )
    assert [c.email for c in second] == ["zoe@htc-example.com"]


def test_count_agrees_with_search_on_the_python_path(three_companies) -> None:
    criteria = CompanyFilter(text="example.com")
    assert three_companies.count(criteria) == len(three_companies.search(criteria))
    assert three_companies.count(criteria) == 3


def test_count_of_an_encrypted_field_filter(three_companies) -> None:
    assert three_companies.count(CompanyFilter(email="alan@")) == 1


def test_contact_search_matches_and_sorts_on_encrypted_fields(
    encryption_on, db_session
) -> None:
    company = CompanyRepository(db_session).upsert(_company("測試公司"))[0]
    repo = ContactRepository(db_session)
    repo.add(company.id, name="王小明", email="ming@example.com", title="經理")
    repo.add(company.id, name="陳大文", email="wen@example.com", title="工程師")
    db_session.commit()

    assert [c.name for c in repo.search("ming@")] == ["王小明"]
    assert [c.name for c in repo.search("陳大")] == ["陳大文"]
    assert len(repo.search()) == 2


def test_activity_body_round_trips(encryption_on, db_session) -> None:
    company = CompanyRepository(db_session).upsert(_company("測試公司"))[0]
    ActivityRepository(db_session).add(company.id, body="與王小明通話，對方要求報價")
    db_session.commit()

    assert _raw(db_session, "activities", "body")[0].startswith(crypto.PREFIX)
    assert "王小明" in ActivityRepository(db_session).for_company(company.id)[0].body


# ------------------------------------------------------------------ 遷移


def _plaintext_row(session, **fields) -> None:
    """繞過 ORM 直接塞一列明文，模擬加密啟用前就存在的資料。

    ORM 的 Python 端預設值（status、created_at 等）在原始 SQL 下不會生效，
    所以這裡補齊那些 NOT NULL 欄位。
    """
    row = {
        "status": RecordStatus.ACTIVE.value,
        "pipeline_stage": "new",
        "priority": "medium",
        "email_verdict": EmailVerdict.UNKNOWN.value,
        "do_not_contact": 0,
        "email_count": 0,
        # 以字串寫入：sqlite3 從 3.12 起不再內建 datetime 轉接器。
        "created_at": "2026-01-01 09:00:00",
        "updated_at": "2026-01-01 09:00:00",
    }
    row.update(fields)
    columns = ", ".join(f'"{k}"' for k in row)
    values = ", ".join(f":{k}" for k in row)
    session.execute(text(f"INSERT INTO companies ({columns}) VALUES ({values})"), row)
    session.commit()


def test_apply_encrypts_pre_existing_plaintext_rows(encryption_on, db_session) -> None:
    _plaintext_row(
        db_session,
        company_name="舊資料公司",
        name_key="舊資料公司",
        dedupe_key="mail:old@example.com",
        email="old@example.com",
        phone="0227231234",
    )
    assert _raw(db_session, "companies", "email") == ["old@example.com"]

    report = encryption.apply(db_session.get_bind())
    assert report.encrypted == 3        # dedupe_key + email + phone（name_key 不加密）

    db_session.expire_all()
    assert _raw(db_session, "companies", "email")[0].startswith(crypto.PREFIX)
    assert CompanyRepository(db_session).get_by_email("old@example.com") is not None


def test_apply_is_idempotent(encryption_on, db_session) -> None:
    _plaintext_row(
        db_session, company_name="舊資料公司", name_key="舊", dedupe_key="n:舊",
        email="old@example.com",
    )
    encryption.apply(db_session.get_bind())
    second = encryption.apply(db_session.get_bind())

    assert second.changed == 0
    db_session.expire_all()
    assert CompanyRepository(db_session).get_by_email("old@example.com") is not None


def test_turning_encryption_off_converts_back_to_plaintext(
    encryption_on, db_session, monkeypatch
) -> None:
    repo = CompanyRepository(db_session)
    repo.upsert(_company("測試公司", email="ceo@example.com", address="台北市"))
    db_session.commit()
    assert _raw(db_session, "companies", "email")[0].startswith(crypto.PREFIX)

    import core.config as config_module
    from database import types

    monkeypatch.setattr(
        config_module,
        "load_config",
        lambda path=None: encryption_on.model_copy(
            update={"database": encryption_on.database.model_copy(
                update={"encrypt": False}
            )}
        ),
    )
    config_module.reset_config()
    types.reset_encryption_state()

    report = encryption.apply(db_session.get_bind())
    assert report.decrypted > 0

    db_session.expire_all()
    assert _raw(db_session, "companies", "email") == ["ceo@example.com"]
    assert _raw(db_session, "companies", "address") == ["台北市"]
    # 明文狀態下等值查詢也要照樣命中。
    assert CompanyRepository(db_session).get_by_email("ceo@example.com") is not None


def test_conversion_keeps_values_that_cannot_be_decrypted(
    encryption_on, db_session, monkeypatch
) -> None:
    """金鑰不對時保留原值——把讀不出來的欄位清成 NULL 等於替使用者刪資料。"""
    repo = CompanyRepository(db_session)
    repo.upsert(_company("測試公司", email="ceo@example.com"))
    db_session.commit()
    stored = _raw(db_session, "companies", "email")[0]

    monkeypatch.setattr(crypto, "decrypt", lambda token: None)
    report = encryption.convert(db_session.get_bind(), to_encrypted=False)

    assert report.decrypted == 0
    assert report.failed
    assert _raw(db_session, "companies", "email") == [stored]


def test_round_trip_through_migration_preserves_every_value(
    encryption_on, db_session
) -> None:
    original = {
        "company_name": "來回測試公司",
        "name_key": "來回測試",
        "dedupe_key": "mail:trip@example.com",
        "email": "trip@example.com",
        "phone": "02-2723-1234",
        "address": "台北市信義區松高路1號9樓之3",
        "contact_person": "王小明",
    }
    _plaintext_row(db_session, **original)

    engine = db_session.get_bind()
    encryption.convert(engine, to_encrypted=True)
    encryption.convert(engine, to_encrypted=False)

    db_session.expire_all()
    for column, value in original.items():
        assert _raw(db_session, "companies", column) == [value]


# ------------------------------------------------------------------ 狀態回報


def test_status_counts_encrypted_and_plaintext(encryption_on, db_session) -> None:
    _plaintext_row(db_session, company_name="明文", name_key="明文",
                   dedupe_key="n:明文", email="clear@example.com")
    CompanyRepository(db_session).upsert(
        _company("密文", dedupe_key="n:密文", email="secret@example.com")
    )
    db_session.commit()

    result = encryption.status(db_session.get_bind())
    assert result.active is True
    assert result.plaintext_values == 2     # 明文那列的 dedupe_key 與 email
    assert result.encrypted_values == 2
    assert result.pending == 2
    assert not result.fully_converted


def _status(**overrides) -> encryption.EncryptionStatus:
    fields = {
        "configured": True,
        "usable": True,
        "encrypted_values": 0,
        "plaintext_values": 0,
    }
    fields.update(overrides)
    return encryption.EncryptionStatus(**fields)


def test_status_disabled_counts_ciphertext_as_pending() -> None:
    """關掉加密之後，還是密文的值就是待轉換的值。"""
    report = _status(configured=False, encrypted_values=7)
    assert report.active is False
    assert report.pending == 7
    assert not report.fully_converted
    assert "已停用" in report.describe()


def test_status_unusable_has_nothing_pending() -> None:
    """做不到的事不算待辦，否則使用者會一直看到永遠清不掉的紅字。"""
    report = _status(usable=False, plaintext_values=5)
    assert report.pending == 0
    assert report.fully_converted
    assert "無法加密" in report.describe()


def test_status_describes_a_partial_conversion() -> None:
    report = _status(encrypted_values=3, plaintext_values=2)
    assert report.pending == 2
    assert "待加密" in report.describe()


def test_status_describes_a_finished_conversion() -> None:
    report = _status(encrypted_values=9)
    assert report.fully_converted
    assert "已加密 9" in report.describe()


def test_status_reports_unconfigured_when_the_config_is_broken(
    db_session, monkeypatch
) -> None:
    """設定讀不出來時當作沒開加密——寧可不轉換，也不要轉一半。"""
    import core.config as config_module

    # 讓 load_config 失敗，get_config 就會在快取失效後跟著失敗——比直接換掉
    # get_config 安全，後者會連 cache_clear 一起弄不見。
    monkeypatch.setattr(
        config_module, "load_config", _raise(RuntimeError("config broken"))
    )
    config_module.reset_config()

    assert encryption.status(db_session.get_bind()).configured is False


def test_status_wraps_database_failures(db_session, monkeypatch) -> None:
    from sqlalchemy.exc import OperationalError

    monkeypatch.setattr(
        encryption, "_tables", _raise(OperationalError("x", {}, Exception()))
    )
    with pytest.raises(CRMError):
        encryption.status(db_session.get_bind())


def test_convert_wraps_database_failures(encryption_on, db_session, monkeypatch) -> None:
    from sqlalchemy.exc import OperationalError

    monkeypatch.setattr(
        encryption, "_tables", _raise(OperationalError("x", {}, Exception()))
    )
    with pytest.raises(CRMError):
        encryption.convert(db_session.get_bind(), to_encrypted=True)


def test_apply_does_nothing_when_encryption_is_unavailable(
    patch_config, db_session
) -> None:
    """沒有保管庫時保持原狀：強行轉換只會把資料弄壞。"""
    _plaintext_row(db_session, company_name="明文", name_key="明文",
                   dedupe_key="n:明文", email="clear@example.com")

    report = encryption.apply(db_session.get_bind())
    assert report.changed == 0
    assert _raw(db_session, "companies", "email") == ["clear@example.com"]


def test_conversion_continues_when_the_automatic_backup_fails(
    encryption_on, db_session, monkeypatch
) -> None:
    """備份失敗只是警告。加密本身比留一份備份更重要，不該被擋下來。"""
    from core.errors import BackupError

    _plaintext_row(db_session, company_name="明文", name_key="明文",
                   dedupe_key="n:明文", email="clear@example.com")
    monkeypatch.setattr(
        "database.backup.create_backup", _raise(BackupError("磁碟滿了"))
    )

    report = encryption.apply(db_session.get_bind())
    assert report.encrypted == 2
    assert _raw(db_session, "companies", "email")[0].startswith(crypto.PREFIX)


def test_startup_refuses_to_open_a_database_whose_key_is_missing(
    encryption_on, db_session, fake_vault
) -> None:
    """換了電腦卻沒帶金鑰時，必須直接擋下來。

    放行的話每個加密欄位都會讀成空白，使用者一存檔就把還救得回來的密文覆蓋成
    空值——那才是真的把資料弄丟。
    """
    CompanyRepository(db_session).upsert(_company("測試公司", email="ceo@example.com"))
    db_session.commit()

    fake_vault.clear()          # 模擬換一台電腦
    crypto.reset_key_cache()

    with pytest.raises(CRMError, match="import-key"):
        encryption.apply(db_session.get_bind())


def test_startup_is_fine_once_the_key_is_imported(
    encryption_on, db_session, fake_vault
) -> None:
    repo = CompanyRepository(db_session)
    repo.upsert(_company("測試公司", email="ceo@example.com"))
    db_session.commit()
    saved = crypto.export_key()

    fake_vault.clear()
    crypto.reset_key_cache()
    crypto.import_key(saved)

    assert encryption.apply(db_session.get_bind()).changed == 0
    db_session.expire_all()
    assert repo.get_by_email("ceo@example.com") is not None


def test_an_empty_database_without_a_key_opens_normally(
    encryption_on, db_session, fake_vault
) -> None:
    """全新安裝還沒有金鑰是正常的，不該被擋。"""
    assert not crypto.has_key()
    assert encryption.apply(db_session.get_bind()).changed == 0


def test_status_reports_a_missing_key(encryption_on, db_session, fake_vault) -> None:
    CompanyRepository(db_session).upsert(_company("測試公司", email="ceo@example.com"))
    db_session.commit()

    fake_vault.clear()
    crypto.reset_key_cache()

    report = encryption.status(db_session.get_bind())
    assert report.key_present is False
    assert report.unreadable is True


def test_primary_key_is_read_from_the_model() -> None:
    assert encryption._primary_key("companies") == "id"


def test_status_when_encryption_is_unavailable(patch_config, db_session) -> None:
    result = encryption.status(db_session.get_bind())
    assert result.configured is True        # 設定要求加密
    assert result.usable is False           # 但這個環境沒有保管庫
    assert result.active is False
    assert result.pending == 0              # 做不到的事不算「待處理」
    assert "無法加密" in result.describe()


# ------------------------------------------------------------- 端到端


def test_crawling_twice_does_not_duplicate_under_encryption(
    encryption_on, db_session
) -> None:
    """整條爬蟲管線跑兩次，第二次應該全部合併而不是全部新增。

    這是加密最可能造成的災難：等值查詢一旦失效，重複執行的爬蟲會把整個目錄
    再寫一遍，而且症狀要到資料變兩倍才會被發現。
    """
    from crawler.pipeline import crawl

    first = crawl(source="sample", config=encryption_on)[0]
    assert first.records_new > 0

    second = crawl(source="sample", config=encryption_on)[0]
    assert second.records_new == 0
    assert second.records_updated == second.records_found

    db_session.expire_all()
    assert len(CompanyRepository(db_session).all()) == first.records_new


def test_export_contains_plaintext(encryption_on, db_session, tmp_path) -> None:
    """匯出的檔案是給人看的，裡面必須是明文而不是密文。"""
    from exporter.service import export_companies

    CompanyRepository(db_session).upsert(
        _company("測試公司", email="ceo@example.com", phone="0227231234")
    )
    db_session.commit()

    target, count = export_companies("csv", path=tmp_path / "out.csv")
    assert count == 1
    content = target.read_text(encoding="utf-8-sig")
    assert "ceo@example.com" in content
    assert crypto.PREFIX not in content


# -------------------------------------------------------------- email_equals


def test_email_equals_uses_lower_on_a_plaintext_column() -> None:
    clause = email_equals(Company.company_name, "  MiXeD  ")
    assert "lower" in str(clause).lower()


def test_email_equals_compares_directly_on_an_encrypted_column() -> None:
    clause = email_equals(Company.email, "a@example.com")
    assert "lower" not in str(clause).lower()
