"""加解密本體與金鑰管理的測試。

:mod:`core.crypto` 的正確性有兩個面向，兩個都會出人命：

* **確定性** —— 相同明文必須得到相同密文，否則等值查詢與唯一索引全部失效。
* **不會回傳看起來像明文的垃圾** —— 解不開的時候必須是 ``None``，而不是一段
  亂碼；亂碼會被上層當成真的地址寫進 Excel 寄出去。

金鑰的取得路徑同樣重要：這裡刻意驗證「保管庫不可用時**不會**默默改用別的地方
存金鑰」，因為那會讓使用者以為資料受保護。
"""

from __future__ import annotations

import base64

import pytest

from core import credentials, crypto
from core.crypto import EncryptionUnavailable


# ---------------------------------------------------------------- 加解密


def test_round_trip(fake_vault) -> None:
    token = crypto.encrypt("ceo@example.com")
    assert token.startswith(crypto.PREFIX)
    assert crypto.decrypt(token) == "ceo@example.com"


def test_round_trip_handles_cjk_and_symbols(fake_vault) -> None:
    for value in ("台北市信義區松高路1號9樓之3", "王小明", "02-2723-1234#210", "a%_'\"b"):
        assert crypto.decrypt(crypto.encrypt(value)) == value


def test_encryption_is_deterministic(fake_vault) -> None:
    """整個資料層都建立在這個性質上。"""
    assert crypto.encrypt("a@example.com") == crypto.encrypt("a@example.com")


def test_different_plaintexts_give_different_ciphertexts(fake_vault) -> None:
    assert crypto.encrypt("a@example.com") != crypto.encrypt("b@example.com")


def test_ciphertext_does_not_contain_the_plaintext(fake_vault) -> None:
    assert "example" not in crypto.encrypt("ceo@example.com")


@pytest.mark.parametrize("value", [None, ""])
def test_empty_values_pass_through(fake_vault, value) -> None:
    """``None`` 與空字串保持原樣，SQL 的 ``IS NULL`` / ``= ''`` 才不用改寫。"""
    assert crypto.encrypt(value) == value
    assert crypto.decrypt(value) == value


def test_encrypting_twice_is_a_no_op(fake_vault) -> None:
    """遷移是冪等的，靠的就是這件事。"""
    once = crypto.encrypt("ceo@example.com")
    assert crypto.encrypt(once) == once


def test_decrypting_plaintext_returns_it_unchanged(fake_vault) -> None:
    """尚未遷移的資料要讀得出來，不能整欄變成 None。"""
    assert crypto.decrypt("ceo@example.com") == "ceo@example.com"


def test_is_encrypted() -> None:
    assert crypto.is_encrypted(crypto.PREFIX + "xxxx")
    assert not crypto.is_encrypted("ceo@example.com")
    assert not crypto.is_encrypted("")
    assert not crypto.is_encrypted(None)


def test_decrypting_with_the_wrong_key_returns_none(fake_vault) -> None:
    """絕不能回傳一段看起來像資料的亂碼。"""
    token = crypto.encrypt("ceo@example.com")

    crypto.reset_key_cache()
    fake_vault[(credentials.SERVICE_NAME, crypto.KEY_NAME)] = base64.urlsafe_b64encode(
        b"\x00" * 32
    ).decode()

    assert crypto.decrypt(token) is None


def test_decrypting_corrupted_ciphertext_returns_none(fake_vault) -> None:
    token = crypto.encrypt("ceo@example.com")
    assert crypto.decrypt(token[:-6] + "AAAAAA") is None
    assert crypto.decrypt(crypto.PREFIX + "not base64 at all!!") is None


# ------------------------------------------------------------------ 金鑰


def test_key_is_generated_once_and_reused(fake_vault) -> None:
    first = crypto.get_key()
    crypto.reset_key_cache()
    assert crypto.get_key() == first
    assert len(fake_vault) == 1, "不該每次都生一把新的金鑰"


def test_key_is_stored_in_the_vault(fake_vault) -> None:
    crypto.get_key()
    assert (credentials.SERVICE_NAME, crypto.KEY_NAME) in fake_vault


def test_get_key_without_create_does_not_generate_one(fake_vault) -> None:
    """``create=False`` 生出新金鑰的話，既有密文就永遠解不開了。"""
    with pytest.raises(EncryptionUnavailable):
        crypto.get_key(create=False)
    assert fake_vault == {}


def test_corrupted_key_in_the_vault_is_reported(fake_vault) -> None:
    fake_vault[(credentials.SERVICE_NAME, crypto.KEY_NAME)] = "!!! not base64 !!!"
    with pytest.raises(EncryptionUnavailable, match="損毀"):
        crypto.get_key()


def test_key_of_the_wrong_length_is_rejected(fake_vault) -> None:
    fake_vault[(credentials.SERVICE_NAME, crypto.KEY_NAME)] = base64.urlsafe_b64encode(
        b"too short"
    ).decode()
    with pytest.raises(EncryptionUnavailable, match="長度"):
        crypto.get_key()


def test_key_is_never_stored_outside_the_vault(fake_vault, monkeypatch) -> None:
    """保管庫寫不進去時要直接失敗，不能偷偷改存到別的地方。"""
    monkeypatch.setattr(
        "core.credentials.set_secret",
        lambda name, value: credentials.SecretSource.UNSET,
    )
    with pytest.raises(EncryptionUnavailable, match="保管庫"):
        crypto.get_key()


# ------------------------------------------------------------ 匯出／匯入

# 金鑰只存在作業系統的保管庫裡，而保管庫不會跟著 crm.db 一起被複製。少了匯出，
# 硬碟壞掉重灌之後連 backups/ 裡的備份都解不開——備份還在，卻一個字都讀不出來。


def test_exported_key_restores_access_on_a_fresh_machine(fake_vault) -> None:
    token = crypto.encrypt("ceo@example.com")
    saved = crypto.export_key()

    # 模擬換一台電腦：資料庫檔案帶過去了，保管庫是空的。
    fake_vault.clear()
    crypto.reset_key_cache()
    assert crypto.has_key() is False
    assert crypto.decrypt(token) is None, "沒有金鑰時要回傳 None，不能整個炸掉"

    crypto.import_key(saved)
    assert crypto.decrypt(token) == "ceo@example.com"


def test_has_key(fake_vault) -> None:
    assert crypto.has_key() is False
    crypto.get_key()
    assert crypto.has_key() is True


def test_export_does_not_invent_a_key(fake_vault) -> None:
    """沒有金鑰時要報錯，不能生一把讓人以為備份成功了。"""
    with pytest.raises(EncryptionUnavailable):
        crypto.export_key()
    assert fake_vault == {}


def test_exported_key_is_plain_text_and_short_enough_to_write_down(fake_vault) -> None:
    crypto.get_key()
    exported = crypto.export_key()
    assert exported.isascii() and "\n" not in exported
    assert len(exported) <= 48


def test_importing_the_same_key_twice_is_fine(fake_vault) -> None:
    crypto.get_key()
    saved = crypto.export_key()
    crypto.import_key(saved)
    crypto.import_key(saved)
    assert crypto.export_key() == saved


def test_importing_a_different_key_is_refused(fake_vault) -> None:
    """覆蓋舊金鑰會讓現有密文永遠讀不出來，不該是打錯字就會發生的事。"""
    crypto.get_key()
    original = crypto.export_key()
    other = base64.urlsafe_b64encode(b"\x01" * 32).decode()

    with pytest.raises(EncryptionUnavailable, match="--force"):
        crypto.import_key(other)
    assert crypto.export_key() == original


def test_importing_a_different_key_with_force_replaces_it(fake_vault) -> None:
    crypto.get_key()
    other = base64.urlsafe_b64encode(b"\x01" * 32).decode()
    crypto.import_key(other, force=True)
    assert crypto.export_key() == other


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("", "不可為空"),
        ("   ", "不可為空"),
        ("!!! not base64 !!!", "格式"),
        (base64.urlsafe_b64encode(b"short").decode(), "長度"),
    ],
)
def test_importing_a_bad_key_is_rejected(fake_vault, value, message) -> None:
    with pytest.raises(EncryptionUnavailable, match=message):
        crypto.import_key(value)


def test_imported_key_takes_effect_immediately(fake_vault) -> None:
    """匯入後不必重開程式——快取要跟著換掉。"""
    crypto.get_key()
    other = base64.urlsafe_b64encode(b"\x02" * 32).decode()
    crypto.import_key(other, force=True)
    assert crypto.get_key() == base64.urlsafe_b64decode(other)


# ------------------------------------------------------------- available()


def test_available_with_a_working_vault(fake_vault) -> None:
    assert crypto.available() is True


def test_available_is_false_without_a_vault(monkeypatch) -> None:
    """測試環境的預設狀態：沒有保管庫就不加密。"""
    monkeypatch.setattr("core.credentials.keyring_available", lambda: False)
    assert crypto.available() is False


def test_available_is_false_when_disabled_by_env(fake_vault, monkeypatch) -> None:
    monkeypatch.setenv(crypto.DISABLE_ENV_VAR, "1")
    assert crypto.encryption_disabled() is True
    assert crypto.available() is False


def test_available_is_false_without_the_cryptography_package(
    fake_vault, monkeypatch
) -> None:
    def _missing():
        raise EncryptionUnavailable("未安裝 cryptography")

    monkeypatch.setattr(crypto, "_aesgcm", _missing)
    assert crypto.available() is False


# ------------------------------------------------------- ciphertext_length


@pytest.mark.parametrize(
    "plaintext",
    [
        "a",
        "a" * 320,
        "王" * 128,
        "台北市信義區松高路1號9樓之3",
        "混合 mixed 內容 123 @example.com",
    ],
)
def test_declared_length_is_an_upper_bound(fake_vault, plaintext) -> None:
    """欄位宣告的長度必須真的裝得下，中文也不例外。"""
    assert len(crypto.encrypt(plaintext)) <= crypto.ciphertext_length(len(plaintext))
