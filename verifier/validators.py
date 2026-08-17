"""Syntax-level validation.

These run on values that have already been through :mod:`verifier.normalize`.
They answer "could this be real?" without touching the network -- the only
network check is the MX lookup in :mod:`verifier.mx`.
"""

from __future__ import annotations

import re

# Deliberately stricter than RFC 5322 (which permits quoted strings and
# comments that no B2B directory ever emits) and looser than a deliverability
# check. Rejects consecutive/leading/trailing dots and single-label domains.
_EMAIL_RE = re.compile(
    r"^[a-z0-9!#$%&'*+/=?^_`{|}~\-]+"
    r"(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~\-]+)*"
    r"@"
    r"(?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+"
    r"[a-z]{2,63}$",
    re.IGNORECASE,
)

_ROLE_LOCAL_PARTS = frozenset(
    {
        "admin", "info", "sales", "service", "support", "contact", "office",
        "webmaster", "postmaster", "noreply", "no-reply", "donotreply",
        "marketing", "hr", "billing", "help", "enquiry", "enquiries",
    }
)

#: 錯誤追蹤與遙測服務的網域。
#:
#: 這些位址是**網頁原始碼裡的機器識別碼**，不是給人收信的信箱。最常見的是
#: Sentry 的 DSN，它會原樣出現在頁面的 JavaScript 裡，長得跟信箱一模一樣：
#:
#:     8eb368c655b84e029ed79ad7a5c1718e@sentry.wixpress.com
#:
#: 用 Wix 架的網站幾乎每一個都有一個，所以抓到的機率很高——而且它會排在
#: 真正的信箱前面被選走（同網域優先那條規則對它沒用，它就是不同網域）。
#: 寄過去不會有人收到，只會累積退信。
_TRACKING_DOMAINS = (
    "sentry.io", "sentry.wixpress.com", "wixpress.com", "sentry-next.wixpress.com",
    "bugsnag.com", "rollbar.com", "newrelic.com", "datadoghq.com",
    "sentry.local", "logrocket.com", "raygun.io",
)

#: 一定不是給人收信的網域：文件範例、規格書、開發用的預留位置。
_PLACEHOLDER_DOMAINS = (
    "example.com", "example.org", "example.net", "example.tw",
    "domain.com", "yourdomain.com", "yourcompany.com", "mydomain.com",
    "email.com", "test.com", "sample.com",
    "schema.org", "w3.org", "localhost", "sentry.local",
)

#: 純十六進位、又長到這個地步的帳號名不是人取的，是程式產生的識別碼。
#: Sentry 的 DSN 公鑰正好是 32 個十六進位字元。
_MACHINE_LOCAL_RE = re.compile(r"^[0-9a-f]{24,}$", re.IGNORECASE)
_UUID_LOCAL_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)

#: 圖檔名稱常被誤認成信箱（``logo@2x.png``）。
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".bmp")


def is_tracking_address(value: str | None) -> bool:
    """這個位址是機器產生的識別碼，不是給人收信的信箱嗎？

    語法上它們完全合法，所以 :func:`is_valid_email` 攔不住——擋它們要看的是
    「這個網域是做什麼的」和「這個帳號名像不像人取的」。

    這一支放在共用的地方，是因為每一條進資料庫的路（爬取、匯入、補齊、Gmail
    收信）都會經過 :class:`~verifier.service.CleaningService`，而重新驗證那條
    路也用它把已經存進去的清掉。
    """
    if not value or "@" not in value:
        return False
    local, _, domain = value.strip().lower().rpartition("@")
    if not local or not domain:
        return False

    if domain.endswith(_IMAGE_SUFFIXES):
        return True
    # 用結尾比對，``o4507.ingest.sentry.io`` 這種子網域才擋得到。
    if any(domain == bad or domain.endswith("." + bad) for bad in _TRACKING_DOMAINS):
        return True
    if any(domain == bad or domain.endswith("." + bad) for bad in _PLACEHOLDER_DOMAINS):
        return True
    return bool(_MACHINE_LOCAL_RE.match(local) or _UUID_LOCAL_RE.match(local))


_URL_RE = re.compile(
    r"^https?://"
    r"(?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}"
    r"(?::\d{1,5})?"
    r"(?:/[^\s]*)?$",
    re.IGNORECASE,
)

_PHONE_RE = re.compile(r"^\+?[\d\-]{7,20}(?:#\d{1,6})?$")

# Weights of the Taiwan unified business number checksum.
_TAX_ID_WEIGHTS = (1, 2, 1, 2, 1, 2, 4, 1)


def is_valid_email(value: str | None) -> bool:
    """True when the address is syntactically plausible and not over-long."""
    if not value:
        return False
    if len(value) > 254:
        return False
    local, _, domain = value.partition("@")
    if not local or len(local) > 64 or not domain or len(domain) > 253:
        return False
    if ".." in value:
        return False
    return bool(_EMAIL_RE.match(value))


def email_domain(value: str | None) -> str | None:
    """Lowercase domain part, or ``None`` when the address is unusable."""
    if not is_valid_email(value):
        return None
    assert value is not None
    return value.rpartition("@")[2].lower()


def is_role_address(value: str | None) -> bool:
    """True for shared mailboxes like ``info@``.

    Not a defect -- for B2B prospecting a role address is often the *only*
    published one -- but worth flagging in the UI.
    """
    if not value or "@" not in value:
        return False
    local = value.partition("@")[0].lower()
    return local in _ROLE_LOCAL_PARTS


def is_disposable_email(value: str | None, disposable_domains: set[str] | None) -> bool:
    """True when the address's domain is a known throwaway provider."""
    domain = email_domain(value)
    if not domain or not disposable_domains:
        return False
    if domain in disposable_domains:
        return True
    # Match subdomains of a listed provider, e.g. mail.mailinator.com.
    return any(domain.endswith("." + d) for d in disposable_domains)


def is_valid_website(value: str | None) -> bool:
    if not value or len(value) > 2048:
        return False
    return bool(_URL_RE.match(value))


def is_valid_phone(value: str | None) -> bool:
    """True for a normalized phone with a plausible digit count."""
    if not value:
        return False
    if not _PHONE_RE.match(value):
        return False
    digits = re.sub(r"\D", "", value.split("#")[0])
    return 7 <= len(digits) <= 15


def is_valid_tax_id(value: str | None) -> bool:
    """Validate a Taiwan unified business number by its checksum.

    Eight digits weighted 1,2,1,2,1,2,4,1; each product is reduced to its digit
    sum and the total must be divisible by 5.

    Special case: when the 7th digit is ``7`` its product is 7*4=28, whose digit
    sum is 10. The Ministry of Finance rule lets that count as either 10 or 1,
    so a total nine lower is equally acceptable.
    """
    if not value or len(value) != 8 or not value.isdigit():
        return False

    digits = [int(c) for c in value]
    total = 0
    for digit, weight in zip(digits, _TAX_ID_WEIGHTS, strict=True):
        product = digit * weight
        total += product // 10 + product % 10

    if total % 5 == 0:
        return True
    return digits[6] == 7 and (total - 9) % 5 == 0


#: 這些**整格就是它**的時候不是公司，是表格的零件。
#:
#: 兩個來源：試算表的合計列與說明列（名冊底下那一行「合計 216 家」），以及
#: 標題列漏進資料裡（欄名變成一筆公司，名字叫「公司名稱」）。兩種都會安靜地
#: 變成一家假公司，然後跟著寄信名單一起出去。
#:
#: 只比對**完全相同**的字串，所以「合計企業有限公司」不受影響。
_NOT_A_COMPANY = frozenset({
    "合計", "總計", "小計", "總和", "總數", "累計", "共計",
    "以上", "備註", "說明", "附註", "註", "其他",
    "無", "未填", "從缺", "n/a", "na", "-", "--", "null", "none",
    "公司名稱", "廠商名稱", "工廠名稱", "企業名稱", "名稱", "公司", "廠商",
    "company", "company name", "name", "total", "subtotal", "remark", "note",
})


def is_valid_company_name(value: str | None) -> bool:
    """Reject obvious non-names: empty, numeric-only, or a single character."""
    if not value:
        return False
    stripped = value.strip()
    if len(stripped) < 2 or len(stripped) > 255:
        return False
    if stripped.lower() in _NOT_A_COMPANY:
        return False
    return bool(re.search(r"[\w一-鿿]", stripped)) and not stripped.isdigit()
