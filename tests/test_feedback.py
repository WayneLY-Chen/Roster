"""問題回報：內容檢查、診斷資訊、以及沒設定 Gmail 時的退路。

這裡最在意的是「使用者按下送出時，到底有什麼東西被寄出去了」。回報是使用者
主動寄給陌生人的東西，夾帶了他沒預期的資訊比功能壞掉更嚴重。
"""

from __future__ import annotations

from urllib.parse import unquote

import pytest

from core.errors import CRMError
from core.feedback import (
    Feedback,
    can_send_directly,
    diagnostics,
    mailto_url,
    send,
    validate,
)


@pytest.fixture
def feedback_config(tmp_config):
    return tmp_config.model_copy(
        update={
            "app": tmp_config.app.model_copy(
                update={"feedback_email": "author@example.com"}
            )
        }
    )


# --------------------------------------------------------------- 內容檢查


def test_an_empty_report_is_refused():
    """空的回報寄出去只會浪費雙方時間。"""
    with pytest.raises(CRMError, match="請先寫下"):
        validate(Feedback(message="   "))


def test_a_malformed_reply_address_is_refused():
    """打錯信箱的話，作者就算想回覆也回不了——寄出前就該講。"""
    with pytest.raises(CRMError, match="不是有效的信箱"):
        validate(Feedback(message="有問題", reply_to="not-an-email"))


def test_leaving_the_reply_address_blank_is_fine():
    validate(Feedback(message="有問題", reply_to=""))


# ------------------------------------------------------------ 診斷資訊


def test_diagnostics_contains_version_and_platform():
    """沒有這些，一則回報通常要來回問三輪才問得出環境。"""
    text = diagnostics()
    assert "Roster" in text
    assert "作業系統" in text
    assert "Python" in text


def test_diagnostics_never_leaks_a_path():
    """完整路徑在 Windows 上一定含使用者帳號名稱。

    回報的人不會預期自己的帳號名稱被夾帶出去——這是他主動寄給陌生人的東西。
    """
    import re

    text = diagnostics()
    assert not re.search(r"[A-Za-z]:[\\/]", text), text
    assert "/Users/" not in text
    assert "/home/" not in text


def test_the_body_carries_the_message_and_the_diagnostics():
    feedback = Feedback(message="匯出的時候會當掉", reply_to="me@example.com")
    body = feedback.body()

    assert "匯出的時候會當掉" in body
    assert "me@example.com" in body
    assert "Roster" in body


def test_the_subject_summarises_the_first_line():
    feedback = Feedback(message="按匯出就沒反應\n\n第二段不該出現在主旨")
    subject = feedback.subject()

    assert subject.startswith("[Roster 回報]")
    assert "按匯出就沒反應" in subject
    assert "第二段" not in subject


def test_a_very_long_first_line_is_truncated_in_the_subject():
    feedback = Feedback(message="問" * 200)
    assert len(feedback.subject()) < 80


def test_an_empty_looking_message_still_produces_a_subject():
    """subject() 不該因為內容只有空白就丟 IndexError。"""
    assert "沒有標題" in Feedback(message="   ").subject()


# --------------------------------------------------------------- 送出路徑


def test_without_gmail_configured_it_cannot_send_directly(feedback_config, monkeypatch):
    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    assert can_send_directly(feedback_config) is False


def test_mailto_url_prefills_the_author_address_subject_and_body(feedback_config):
    feedback = Feedback(message="有個地方怪怪的", reply_to="me@example.com")
    url = mailto_url(feedback, feedback_config)

    assert url.startswith("mailto:author@example.com?")
    decoded = unquote(url)
    assert "有個地方怪怪的" in decoded
    assert "me@example.com" in decoded


def test_mailto_url_validates_before_building(feedback_config):
    """按下去才發現內容是空的，不如一開始就擋。"""
    with pytest.raises(CRMError):
        mailto_url(Feedback(message=""), feedback_config)


def test_send_goes_to_the_author_with_the_attachments(feedback_config, monkeypatch):
    """真正要保證的：收件人是作者，而且截圖真的掛上去了。"""
    sent: dict[str, object] = {}

    class _FakeSender:
        def __init__(self, config=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def send(self, message, attachments=None):
            sent["to"] = message.to_address
            sent["subject"] = message.subject
            sent["body"] = message.body
            sent["attachments"] = attachments

    monkeypatch.setattr("gmail.sender.SmtpSender", _FakeSender)
    monkeypatch.setattr(
        "gmail.attachments.load_for_sending",
        lambda names, config=None: [(n, b"data", "image/png") for n in names],
    )

    send(
        Feedback(message="這裡壞了", reply_to="me@example.com", attachments=["截圖.png"]),
        feedback_config,
    )

    assert sent["to"] == "author@example.com"
    assert "這裡壞了" in sent["subject"]
    assert [name for name, _data, _mime in sent["attachments"]] == ["截圖.png"]
