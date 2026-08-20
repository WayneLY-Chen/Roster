"""憑證存取。

Gmail 應用程式密碼優先存在**作業系統的憑證保管庫**（Windows 是「認證管理員」、
macOS 是 Keychain、Linux 是 Secret Service），而不是專案資料夾裡的 `.env`。

為什麼要這樣：`.env` 是明文檔案，就躺在專案目錄中。只要它被雲端同步、被打包
寄出、或哪天有人把 `.gitignore` 改壞，密碼就外流了。存進系統保管庫之後，
專案資料夾裡完全不會出現密碼，整個目錄就能安全地上 git。

`.env` 仍然可用，作為向後相容與 CI／無圖形介面環境的退路，但只在保管庫沒有
資料時才會被讀取，並且 :func:`describe` 會明白告訴使用者現在用的是哪一種。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum

from core.constants import LogCategory
from core.logging_setup import get_logger

log = get_logger(LogCategory.GUI)

#: 憑證保管庫中的服務名稱。
SERVICE_NAME = "Roster"

#: 改名前用的服務名稱。
#:
#: 這個字串是資料能不能救回來的關鍵：資料庫的個資欄位用的加密金鑰只存在系統
#: 憑證保管庫，掛在服務名底下。改名時如果直接換掉它，程式會找不到金鑰，
#: `crm.db` 與 `backups/` 裡的內容就全部解不開——而且是安靜地解不開。
#:
#: 所以不能直接換：新名字查不到時，回頭用舊名字查一次，查到就搬過來。
LEGACY_SERVICE_NAMES: tuple[str, ...] = ("TaiwanB2BCRM",)

#: 設成 1/true 可完全停用系統保管庫，只使用環境變數。
#:
#: 測試一定要設這個。否則在「已經設定好 Gmail」的機器上，測試用
#: monkeypatch 塞的假密碼會被保管庫裡的真密碼蓋掉——測試會讀到開發者
#: 本人的憑證，結果因人而異。
DISABLE_ENV_VAR = "CRM_DISABLE_KEYRING"

#: 邏輯名稱 -> 對應的環境變數名稱（退路用）。
SECRET_ENV_VARS: dict[str, str] = {
    "gmail_address": "GMAIL_ADDRESS",
    "gmail_app_password": "GMAIL_APP_PASSWORD",
    "crawler_contact": "CRM_CRAWLER_CONTACT",
    # 「補齊公司資料」找官網用的搜尋金鑰。兩個都是選填——沒填就用免金鑰的
    # DuckDuckGo，見 crawler/websearch.py。搜尋引擎 ID 不算密碼，但跟金鑰
    # 一起存管理起來單純，而且它同樣沒有理由出現在專案資料夾裡。
    "brave_search_key": "BRAVE_SEARCH_KEY",
    "google_search_key": "GOOGLE_SEARCH_KEY",
    "google_search_cx": "GOOGLE_SEARCH_CX",
    # OpenRouter 的金鑰。選填——不填就只能用本機的 Ollama，見 ai/provider.py。
    "openrouter_key": "OPENROUTER_API_KEY",
    # Anthropic 官方 API 的金鑰。注意這跟 Claude 的訂閱方案是兩回事，
    # 帳單也是分開的——見 ai/provider.py 的 AnthropicProvider 說明。
    "anthropic_key": "ANTHROPIC_API_KEY",
}

try:  # pragma: no cover - 取決於執行環境
    import keyring
    import keyring.errors

    _KEYRING_IMPORTED = True
except ImportError:  # pragma: no cover
    _KEYRING_IMPORTED = False


class SecretSource(str, Enum):
    """這個密碼實際上是從哪裡讀到的。"""

    KEYRING = "系統憑證保管庫"
    ENV = ".env 檔（明文）"
    UNSET = "尚未設定"


@dataclass(frozen=True, slots=True)
class SecretStatus:
    """給設定頁顯示用的狀態，永遠不包含密碼本身。"""

    name: str
    source: SecretSource
    hint: str = ""

    @property
    def is_set(self) -> bool:
        return self.source is not SecretSource.UNSET

    @property
    def is_secure(self) -> bool:
        return self.source is SecretSource.KEYRING


def keyring_available() -> bool:
    """系統憑證保管庫是否真的能用。

    光是 import 成功不夠：Linux 上沒有 Secret Service 時，keyring 會退化成
    一個會丟例外的後端，所以這裡實際問過後端才回答。
    """
    if not _KEYRING_IMPORTED:
        return False
    if os.getenv(DISABLE_ENV_VAR, "").strip().lower() in ("1", "true", "yes"):
        return False
    try:
        backend = keyring.get_keyring()
    except Exception:  # pragma: no cover
        return False
    return "fail" not in backend.__class__.__name__.lower()


def _migrate_from_legacy(name: str) -> str | None:
    """在舊服務名底下找這個密碼，找到就搬到新服務名底下並回傳。

    程式改過名字，但使用者的保管庫裡還是舊名字。沒有這一步，加密金鑰就會
    憑空消失，資料庫與備份全部解不開。搬完保留舊的那一份，萬一使用者還要
    退回舊版也還在。
    """
    for legacy in LEGACY_SERVICE_NAMES:
        try:
            value = keyring.get_password(legacy, name)
        except Exception:  # pragma: no cover - 後端故障
            continue
        if not value:
            continue
        try:
            keyring.set_password(SERVICE_NAME, name, value)
            log.info("已把「{}」從舊的保管庫名稱 {} 搬到 {}", name, legacy, SERVICE_NAME)
        except Exception as exc:  # pragma: no cover
            log.warning("搬移「{}」到新的保管庫名稱失敗，這次先直接沿用舊的：{}", name, exc)
        return value.strip()
    return None


def get_secret(name: str) -> str:
    """讀取一個密碼：先問保管庫，再退回環境變數。查不到回傳空字串。"""
    if keyring_available():
        try:
            value = keyring.get_password(SERVICE_NAME, name)
        except Exception as exc:  # pragma: no cover - 後端故障
            log.warning("讀取憑證保管庫失敗（{}），改用環境變數：{}", name, exc)
        else:
            if value:
                return value.strip()
            migrated = _migrate_from_legacy(name)
            if migrated:
                return migrated

    env_var = SECRET_ENV_VARS.get(name, name.upper())
    return os.getenv(env_var, "").strip()


def set_secret(name: str, value: str) -> SecretSource:
    """寫入一個密碼。回傳它實際被存到哪裡。

    保管庫不可用時**不會**退而寫入 `.env`——那等於在使用者以為變安全的時候，
    偷偷把密碼寫成明文。此時回傳 :attr:`SecretSource.UNSET`，由呼叫端據實告知。
    """
    value = (value or "").strip()
    if not value:
        delete_secret(name)
        return SecretSource.UNSET

    if not keyring_available():
        log.warning("系統憑證保管庫不可用，無法安全儲存 {}", name)
        return SecretSource.UNSET

    try:
        keyring.set_password(SERVICE_NAME, name, value)
    except Exception as exc:
        log.error("寫入憑證保管庫失敗（{}）：{}", name, exc)
        return SecretSource.UNSET

    log.info("{} 已存入系統憑證保管庫", name)
    return SecretSource.KEYRING


def delete_secret(name: str) -> bool:
    """從保管庫刪除一個密碼。`.env` 中的值不受影響（那要由使用者自己刪）。"""
    if not keyring_available():
        return False
    try:
        keyring.delete_password(SERVICE_NAME, name)
    except Exception:
        return False
    log.info("{} 已自系統憑證保管庫刪除", name)
    return True


def describe(name: str) -> SecretStatus:
    """回報某個密碼的設定狀態，供設定頁顯示。不會回傳密碼內容。"""
    if keyring_available():
        try:
            if keyring.get_password(SERVICE_NAME, name):
                return SecretStatus(name, SecretSource.KEYRING, "已安全儲存於系統，專案資料夾中沒有這筆密碼")
        except Exception:  # pragma: no cover
            pass

    env_var = SECRET_ENV_VARS.get(name, name.upper())
    if os.getenv(env_var, "").strip():
        return SecretStatus(
            name,
            SecretSource.ENV,
            f"目前存在 .env 的 {env_var}（明文）。建議在此重新輸入一次，改存到系統保管庫，"
            "然後把該行從 .env 刪除。",
        )

    return SecretStatus(name, SecretSource.UNSET, "尚未設定")


def migrate_env_to_keyring() -> list[str]:
    """把 `.env` 裡既有的密碼搬進保管庫。回傳搬移成功的名稱。

    不會刪除 `.env` 的內容——刪掉使用者的檔案不是這支函式該做的決定，
    只回報結果由使用者自行處理。
    """
    moved: list[str] = []
    if not keyring_available():
        return moved

    for name, env_var in SECRET_ENV_VARS.items():
        if name == "crawler_contact":
            continue  # 這不是密碼，留在 .env 就好
        value = os.getenv(env_var, "").strip()
        if value and set_secret(name, value) is SecretSource.KEYRING:
            moved.append(name)
    return moved
