"""HTML 信件的組裝測試。

寄出去的信收不回來，所以這裡盯的是「收件者實際會看到什麼」：HTML 信必須同時附
一份純文字（不是每個信箱都顯示 HTML，而且純 HTML 的信比較容易被判定成垃圾郵件），
圖片必須以 CID 附件送出（Gmail 會把 data: URI 的圖片擋掉）。
"""

from __future__ import annotations

import pytest

from core.config import AppConfig
from gmail.sender import SmtpSender


@pytest.fixture
def sender(tmp_path):
    """一個不連線的 sender，只用來組 MIME。"""
    config = AppConfig.model_validate(
        {"mailer": {"templates_dir": str(tmp_path / "mail")}}
    )
    return SmtpSender(config)


@pytest.fixture
def images_dir(tmp_path):
    target = tmp_path / "mail" / "images"
    target.mkdir(parents=True)
    return target


def _build(sender, body: str):
    from email.message import EmailMessage as MimeMessage

    mime = MimeMessage()
    mime["To"] = "someone@example.com"
    sender._set_body(mime, body)
    return mime


# 一個最小的合法 PNG。
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


# --------------------------------------------------------------------- 純文字


def test_plain_body_stays_a_single_part(sender) -> None:
    mime = _build(sender, "您好，這是純文字信件。")
    assert not mime.is_multipart()
    assert "您好" in mime.get_content()


# ------------------------------------------------------------------- HTML 信


def test_html_body_carries_a_plain_text_alternative(sender) -> None:
    mime = _build(sender, "<p>您好</p><p><strong>謝謝</strong></p>")
    types = {part.get_content_type() for part in mime.walk()}
    assert "text/plain" in types
    assert "text/html" in types


def test_plain_alternative_has_no_tags(sender) -> None:
    mime = _build(sender, "<p>您好</p>")
    plain = mime.get_body(preferencelist=("plain",)).get_content()
    assert "您好" in plain and "<p>" not in plain


def test_html_part_keeps_the_markup(sender) -> None:
    mime = _build(sender, "<p><strong>粗體</strong></p>")
    html = mime.get_body(preferencelist=("html",)).get_content()
    assert "<strong>" in html


# --------------------------------------------------------------------- 圖片


def test_image_is_attached_as_cid(sender, images_dir) -> None:
    """Gmail 會把 data: URI 的圖片擋掉，所以一定要走 CID。"""
    (images_dir / "logo.png").write_bytes(_PNG)

    mime = _build(sender, '<p><img src="images/logo.png"></p>')
    html = mime.get_body(preferencelist=("html",)).get_content()
    assert "cid:" in html
    assert "images/logo.png" not in html

    images = [p for p in mime.walk() if p.get_content_maintype() == "image"]
    assert len(images) == 1
    assert images[0].get_payload(decode=True) == _PNG


def test_cid_in_the_html_matches_the_attachment(sender, images_dir) -> None:
    (images_dir / "logo.png").write_bytes(_PNG)
    mime = _build(sender, '<p><img src="images/logo.png"></p>')

    html = mime.get_body(preferencelist=("html",)).get_content()
    cid = html.split("cid:")[1].split('"')[0]
    attached = [p for p in mime.walk() if p.get_content_maintype() == "image"][0]
    assert attached["Content-ID"].strip("<>") == cid


def test_a_missing_image_does_not_stop_the_send(sender, images_dir) -> None:
    """壞掉的一張 logo 不該讓整批開發信寄不出去。"""
    mime = _build(sender, "<p>您好</p><p><img src=\"images/gone.png\"></p>")
    html = mime.get_body(preferencelist=("html",)).get_content()
    assert "您好" in html
    assert not [p for p in mime.walk() if p.get_content_maintype() == "image"]


def test_remote_images_are_left_alone(sender) -> None:
    mime = _build(sender, '<p><img src="https://example.com/a.png"></p>')
    html = mime.get_body(preferencelist=("html",)).get_content()
    assert "https://example.com/a.png" in html


def test_image_path_cannot_escape_the_images_folder(sender, images_dir, tmp_path) -> None:
    """樣板是可編輯的檔案，不能讓它把 .env 變成信件附件。"""
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"GMAIL_APP_PASSWORD=hunter2")

    mime = _build(sender, '<p><img src="../../secret.png"></p>')
    assert not [p for p in mime.walk() if p.get_content_maintype() == "image"]
    assert b"hunter2" not in mime.as_bytes()


def test_several_images_get_distinct_cids(sender, images_dir) -> None:
    (images_dir / "a.png").write_bytes(_PNG)
    (images_dir / "b.png").write_bytes(_PNG)

    mime = _build(sender, '<p><img src="images/a.png"><img src="images/b.png"></p>')
    html = mime.get_body(preferencelist=("html",)).get_content()
    cids = [chunk.split('"')[0] for chunk in html.split("cid:")[1:]]
    assert len(cids) == 2 and len(set(cids)) == 2


# ------------------------------------------------------- 變數代入與 HTML 逸出


def test_placeholders_are_escaped_in_an_html_template() -> None:
    """「A&B 企業社」這種公司名很常見，不逸出就會產生壞掉的 HTML。"""
    from gmail.templates import render

    result = render("<p>{company_name} 您好</p>", {"company_name": "A&B <企業社>"})
    assert "&amp;" in result and "&lt;企業社&gt;" in result


def test_placeholders_are_not_escaped_in_a_plain_text_template() -> None:
    """純文字樣板不該冒出 &amp; 這種東西。"""
    from gmail.templates import render

    result = render("{company_name} 您好", {"company_name": "A&B 企業社"})
    assert result == "A&B 企業社 您好"


def test_a_company_name_cannot_inject_markup() -> None:
    from gmail.templates import render

    result = render(
        "<p>{company_name}</p>", {"company_name": "</p><script>alert(1)</script>"}
    )
    assert "<script>" not in result
