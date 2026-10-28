"""A deliberately dumb ``{placeholder}`` template engine for outbound mail.

No templating library is introduced: substitution is plain ``str`` find/replace
driven by a regex, so a saved ``.txt`` file is exactly what a human reviewer
sees rendered. An unknown placeholder is a hard error (:class:`GmailError`)
rather than a silently unreplaced ``{typo}`` that would otherwise go out to a
real prospect.

Templates live under ``mailer.resolved_templates_dir`` as plain text files.
The first line is ``Subject: ...``; a blank line separates it from the body.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path

from core.config import AppConfig, get_config
from core.errors import GmailError
from core.schemas import CompanyView

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

#: 代入純文字樣板的值裡，任何長得像 HTML 標籤的片段（``<a href=…>``、``</b>``）。
#: 只認「``<`` 後面緊接英文字母或 ``/``」，所以 ``<3``、``< 100`` 不受影響。
_VALUE_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")

# Placeholder name -> Chinese description shown in the GUI's helper panel.
_PLACEHOLDERS: dict[str, str] = {
    "company_name": "公司名稱",
    "english_name": "公司英文名稱",
    "products": "主要產品",
    "contact_person": "聯絡人姓名",
    "industry": "產業別",
    "email": "電子信箱",
    "phone": "電話",
    "website": "官方網站",
    "address": "地址",
    "city": "城市（取自地址開頭的縣市）",
}

# Sensible stand-ins for fields a company record often lacks, so a missing
# value never leaves a bare, awkward gap in the rendered message.
_DEFAULTS: dict[str, str] = {
    "company_name": "貴公司",
    "contact_person": "您好",
    "industry": "貴產業",
    "email": "",
    "phone": "",
    "website": "",
    "address": "",
    "city": "",
}

_CITY_RE = re.compile(r"^\D{2,3}[縣市]")


def available_placeholders() -> dict[str, str]:
    """Placeholder name -> Chinese description, for the GUI to display."""
    return dict(_PLACEHOLDERS)


def _company_fields(company: CompanyView | dict) -> dict[str, str]:
    data = company.model_dump() if isinstance(company, CompanyView) else dict(company)

    fields = dict(_DEFAULTS)
    for key in _PLACEHOLDERS:
        value = data.get(key)
        if value:
            fields[key] = str(value)

    if not fields["city"] and data.get("address"):
        match = _CITY_RE.match(str(data["address"]))
        if match:
            fields["city"] = match.group(0)

    return fields


def render(template: str, company: CompanyView | dict) -> str:
    """Substitute every ``{placeholder}`` with data from ``company``.

    Raises :class:`GmailError` listing any placeholder that is not one of
    :func:`available_placeholders` -- never sends a template with a token left
    unreplaced.

    In an HTML template the substituted values are escaped. Real company names
    contain ``&`` often enough ("A&B 企業社"), and an unescaped one would at
    best produce invalid markup and at worst let a stored ``<`` silently
    rearrange the message.
    """
    from gmail.richtext import looks_like_html

    fields = _company_fields(company)

    used = set(_PLACEHOLDER_RE.findall(template))
    unknown = sorted(name for name in used if name not in _PLACEHOLDERS)
    if unknown:
        known = ", ".join(f"{{{name}}}" for name in _PLACEHOLDERS)
        bad = ", ".join(f"{{{name}}}" for name in unknown)
        raise GmailError(f"樣板包含未知的佔位符：{bad}。可用的佔位符為：{known}")

    escape_values = looks_like_html(template)

    def _replace(match: re.Match[str]) -> str:
        value = fields.get(match.group(1), "")
        if escape_values:
            return html.escape(value)
        # 純文字樣板：代入的值多半是名錄上爬來的。值裡的標籤不拿掉的話，
        # 寄出時會被當成 HTML 信，等於讓刊登者在你的信裡放連結或追蹤圖片。
        return _VALUE_TAG_RE.sub("", value)

    return _PLACEHOLDER_RE.sub(_replace, template)


@dataclass(slots=True)
class MailTemplate:
    """One saved template: a subject line and a body, both may hold placeholders."""

    name: str
    subject: str
    body: str


def _templates_dir(config: AppConfig | None = None) -> Path:
    cfg = config or get_config()
    directory = cfg.mailer.resolved_templates_dir
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _template_path(name: str, config: AppConfig | None = None) -> Path:
    """把樣板名稱轉成實際路徑，並確保它真的在樣板資料夾裡面。

    樣板名稱來自使用者在介面上打的字（郵件頁的「新增樣板」），所以直接把它
    接在資料夾後面組路徑是不對的：``../../x`` 會跑到資料夾外面去，而
    :func:`save_template` 是**寫入**——寫到外面比讀到外面嚴重得多。

    這不是一個現在打得到的洞（輸入只來自本機使用者自己的 GUI），修它的理由是
    一致性：這個專案裡另外三條「拿別人給的名字去組路徑」的路
    ——:func:`gmail.attachments.resolve`、:func:`database.backup.delete_backup`、
    以及 :mod:`gmail.sender` 處理內文圖片——全都做了同一件事，只有這裡漏掉。
    三個地方守得住、第四個沒有，那個不一致本身就是問題：下一個人照著隔壁的
    寫法改這裡時，會以為圍堵已經有了。

    做法跟那三處一致：先只取檔名部分（丟掉任何目錄成分），再確認組出來的路徑
    真的落在樣板資料夾底下。
    """
    directory = _templates_dir(config)
    # 去掉頭尾的點與空白，跟 gmail.attachments.safe_name 同一個理由：Path.name
    # 對 ".." 回傳的是 ".."（不是空字串），照它組出來會得到一個叫 "...txt" 的
    # 檔案——有被圍在資料夾裡，但那個名字在樣板清單上是看不懂的東西。Windows
    # 本身也不接受結尾是點或空白的檔名。
    stem = Path(name).name.strip(". ")
    if not stem:
        raise GmailError(f"樣板名稱不能只有路徑符號、點或空白：{name!r}")

    candidate = (directory / f"{stem}.txt").resolve()
    try:
        candidate.relative_to(directory.resolve())
    except ValueError as exc:      # pragma: no cover - 上面取過檔名，幾乎到不了
        raise GmailError(f"樣板路徑不在樣板資料夾內：{name!r}") from exc
    return candidate


def list_templates(config: AppConfig | None = None) -> list[str]:
    """Names (without ``.txt``) of every template on disk, alphabetically."""
    return sorted(path.stem for path in _templates_dir(config).glob("*.txt"))


def load_template(name: str, config: AppConfig | None = None) -> MailTemplate:
    """Read one template file. Raises :class:`GmailError` if missing/malformed."""
    path = _template_path(name, config)
    if not path.exists():
        raise GmailError(f"找不到樣板：{name!r}（預期路徑：{path}）")
    subject, body = _parse_template(path.read_text(encoding="utf-8"), name)
    return MailTemplate(name=name, subject=subject, body=body)


def save_template(
    name: str, subject: str, body: str, config: AppConfig | None = None
) -> Path:
    """Write (or overwrite) a template file. Returns the path written."""
    path = _template_path(name, config)
    path.write_text(f"Subject: {subject}\n\n{body}\n", encoding="utf-8")
    return path


def _parse_template(raw: str, name: str) -> tuple[str, str]:
    lines = raw.splitlines()
    if not lines or not lines[0].startswith("Subject:"):
        raise GmailError(f"樣板 {name!r} 格式錯誤：第一行必須是 'Subject: 主旨'")
    subject = lines[0][len("Subject:") :].strip()
    rest = lines[1:]
    if rest and rest[0].strip() == "":
        rest = rest[1:]
    body = "\n".join(rest).strip("\n")
    return subject, body
