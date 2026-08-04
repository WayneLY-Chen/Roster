"""Normalization of scraped text into canonical, comparable values.

Every function is pure and total: it takes messy input and returns either a
clean value or ``None``. Nothing here raises on bad data -- validation is
:mod:`verifier.validators`' job, and it runs on the *normalized* value.

Taiwan-specific handling: full-width characters (common when a site's CMS
pastes from Word), ``+886`` phone prefixes, ``台``/``臺`` spelling variants, and
company-form suffixes such as ``股份有限公司``.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Legal-form suffixes and decorations dropped when building a comparison key.
_COMPANY_SUFFIXES = (
    "股份有限公司",
    "有限責任公司",
    "有限公司",
    "合夥事業",
    "獨資商號",
    "企業社",
    "工作室",
    "商行",
    "行號",
    "公司",
    "co., ltd.",
    "co.,ltd.",
    "co ltd",
    "company limited",
    "limited",
    "corporation",
    "corp.",
    "corp",
    "inc.",
    "inc",
    "ltd.",
    "ltd",
    "llc",
    "gmbh",
)

# Bracketed legal-form markers, e.g. 台積電（股）公司.
_BRACKET_NOISE = re.compile(r"[（(]\s*(股|有|無)\s*[)）]")
_WHITESPACE = re.compile(r"\s+")
_NON_NAME_CHARS = re.compile(r"[^\w一-鿿]+", re.UNICODE)

# Taiwan landline area codes, longest first so 0826 wins over 08.
_AREA_CODES = (
    "0836", "0826", "089", "082", "049", "037",
    "02", "03", "04", "05", "06", "07", "08",
)

_EMAIL_IN_TEXT = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
_ANGLE_EMAIL = re.compile(r"<\s*([^>]+?)\s*>")
_TRACKING_PARAMS = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid", "_ga")


def to_halfwidth(value: str) -> str:
    """Fold full-width forms to their ASCII equivalents (NFKC)."""
    return unicodedata.normalize("NFKC", value)


def squeeze(value: str) -> str:
    """Collapse all whitespace runs to a single space and trim."""
    return _WHITESPACE.sub(" ", value).strip()


def clean_text(value: str | None) -> str | None:
    """Generic tidy-up for free text. Empty results become ``None``."""
    if value is None:
        return None
    cleaned = squeeze(to_halfwidth(str(value)))
    # Strip zero-width and BOM characters that survive NFKC.
    cleaned = cleaned.replace("​", "").replace("﻿", "").strip()
    return cleaned or None


def normalize_company_name(value: str | None) -> str | None:
    """Display form of a company name: tidy, but nothing removed."""
    cleaned = clean_text(value)
    if not cleaned:
        return None
    # Common scrape artefacts.
    cleaned = cleaned.strip("·-–—|,、 ")
    return cleaned or None


def company_name_key(value: str | None) -> str:
    """Comparison key for a company name.

    Drops legal-form suffixes, punctuation, spacing and ``台``/``臺`` spelling
    so that ``台灣大電力研究試驗中心股份有限公司`` and ``臺灣大電力研究試驗中心``
    collapse to the same key. Returns ``""`` when there is nothing to compare.
    """
    cleaned = normalize_company_name(value)
    if not cleaned:
        return ""

    key = _BRACKET_NOISE.sub("", cleaned).lower()
    # Repeat: names like "... 有限公司(股)" carry more than one suffix.
    changed = True
    while changed:
        changed = False
        for suffix in _COMPANY_SUFFIXES:
            if key.endswith(suffix) and len(key) > len(suffix):
                key = key[: -len(suffix)].strip(" .,-·")
                changed = True
    key = key.replace("臺", "台")
    key = _NON_NAME_CHARS.sub("", key)
    return key


def normalize_email(value: str | None) -> str | None:
    """Extract and canonicalize a single address.

    Handles ``mailto:`` links, ``Name <addr@example.com>`` display forms, and
    addresses embedded in a sentence. Returns lowercase ``local@domain``.
    """
    if value is None:
        return None
    raw = to_halfwidth(str(value)).strip()
    if not raw:
        return None

    if raw.lower().startswith("mailto:"):
        raw = raw[len("mailto:") :]
    raw = raw.split("?", 1)[0]          # mailto:a@b.com?subject=...

    angled = _ANGLE_EMAIL.search(raw)
    if angled:
        raw = angled.group(1)

    # Obfuscations seen on directory sites.
    raw = re.sub(r"\s*\(at\)\s*|\s*\[at\]\s*|\s+at\s+", "@", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*\(dot\)\s*|\s*\[dot\]\s*", ".", raw, flags=re.IGNORECASE)
    raw = raw.replace(" ", "")

    match = _EMAIL_IN_TEXT.search(raw)
    if not match:
        return None
    address = match.group(0).lower().strip(".")
    local, _, domain = address.partition("@")
    if not local or not domain:
        return None
    return f"{local}@{domain}"


def extract_emails(text: str | None) -> list[str]:
    """Every distinct address in a blob of text, in order of appearance."""
    if not text:
        return []
    found: list[str] = []
    for candidate in _EMAIL_IN_TEXT.findall(to_halfwidth(text)):
        address = normalize_email(candidate)
        if address and address not in found:
            found.append(address)
    return found


def normalize_phone(value: str | None) -> str | None:
    """Canonicalize a Taiwanese phone number.

    ``+886 2 2723 1234`` and ``(02)27231234`` both become ``02-27231234``.
    Mobiles become ``09XX-XXX-XXX``. An extension is preserved as ``#123``.
    Numbers that do not look Taiwanese are returned digit-normalized rather
    than discarded -- a foreign number is still a real contact.
    """
    if value is None:
        return None
    raw = to_halfwidth(str(value)).strip()
    if not raw:
        return None

    extension = ""
    ext_match = re.search(
        r"(?:#|ext\.?|轉|分機)\s*(\d{1,6})", raw, flags=re.IGNORECASE
    )
    if ext_match:
        extension = f"#{ext_match.group(1)}"
        raw = raw[: ext_match.start()]

    international = raw.lstrip().startswith("+") and not raw.lstrip().startswith("+886")
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None

    if digits.startswith("886"):
        digits = "0" + digits[3:]
    elif international:
        return f"+{digits}{extension}"

    if not digits.startswith("0"):
        digits = "0" + digits if len(digits) in (8, 9) else digits

    if digits.startswith("09") and len(digits) == 10:
        return f"{digits[:4]}-{digits[4:7]}-{digits[7:]}{extension}"

    if digits.startswith("0800") and len(digits) == 10:
        return f"0800-{digits[4:7]}-{digits[7:]}{extension}"

    for area in _AREA_CODES:
        if digits.startswith(area) and len(digits) > len(area):
            return f"{area}-{digits[len(area):]}{extension}"

    return f"{digits}{extension}"


def normalize_website(value: str | None) -> str | None:
    """Canonicalize a URL: scheme added, host lowercased, tracking stripped."""
    if value is None:
        return None
    raw = clean_text(value)
    if not raw:
        return None
    raw = raw.strip("<>\"' ")
    if raw.lower() in ("n/a", "na", "none", "-", "無", "沒有"):
        return None
    if raw.lower().startswith(("mailto:", "tel:", "javascript:")):
        return None

    if "://" not in raw:
        raw = "https://" + raw.lstrip("/")

    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    if not parts.netloc or "." not in parts.netloc:
        return None

    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return None
    netloc = parts.netloc.lower().rstrip(".")

    query = urlencode(
        [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith(_TRACKING_PARAMS)
        ]
    )
    path = parts.path.rstrip("/") if parts.path != "/" else ""
    return urlunsplit((scheme, netloc, path, query, ""))


def website_host(value: str | None) -> str | None:
    """Registrable host of a URL without ``www.``; used for dedupe keys."""
    normalized = normalize_website(value)
    if not normalized:
        return None
    host = urlsplit(normalized).netloc
    host = host.split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def normalize_address(value: str | None) -> str | None:
    """Tidy a Taiwanese address; keeps the original wording."""
    cleaned = clean_text(value)
    if not cleaned:
        return None
    cleaned = re.sub(r"^(?:地址|Address)\s*[:：]\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^\d{3,6}\s+(?=[一-鿿])", "", cleaned)  # leading zip
    cleaned = cleaned.replace(" ", "") if _is_mostly_cjk(cleaned) else cleaned
    return cleaned or None


def _is_mostly_cjk(value: str) -> bool:
    letters = [c for c in value if not c.isspace()]
    if not letters:
        return False
    cjk = sum(1 for c in letters if "一" <= c <= "鿿")
    return cjk / len(letters) > 0.5


def normalize_tax_id(value: str | None) -> str | None:
    """Extract an 8-digit Taiwan unified business number (統一編號)."""
    if value is None:
        return None
    digits = re.sub(r"\D", "", to_halfwidth(str(value)))
    match = re.search(r"\d{8}", digits)
    return match.group(0) if match else None


def normalize_person_name(value: str | None) -> str | None:
    """Clean a contact person's name, dropping honorific/title decoration."""
    cleaned = clean_text(value)
    if not cleaned:
        return None
    cleaned = re.sub(
        r"^(?:聯絡人|聯絡窗口|Contact(?:\s*Person)?)\s*[:：]\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned.strip(" ,、·") or None


def normalize_industry(value: str | None) -> str | None:
    cleaned = clean_text(value)
    if not cleaned:
        return None
    cleaned = re.sub(r"^(?:產業|行業|類別|Industry)\s*[:：]\s*", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip(" ,、/|") or None
