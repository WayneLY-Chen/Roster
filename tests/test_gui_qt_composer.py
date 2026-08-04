"""Tests for gui_qt/composer.py -- the QTextEdit-based replacement for the
Tk hand-rolled rich-text editor in gui/composer.py.

The part worth being paranoid about is the seam with gmail/sender.py, which
this task is explicitly not allowed to modify: the HTML this composer
produces has to be exactly the shape ``SmtpSender._resolve_images()`` already
expects (``<img src="images/<filename>">``), and the plain-text fallback used
by ``gmail.richtext.html_to_plain_text()`` must not be polluted by the
``<style>`` block ``QTextEdit.toHtml()`` puts in ``<head>``.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtGui import QImage, QTextDocument  # noqa: E402
from PySide6.QtWidgets import QApplication, QTextEdit  # noqa: E402

from gmail.richtext import looks_like_html  # noqa: E402
from gmail.sender import SmtpSender  # noqa: E402
from gui_qt.composer import (  # noqa: E402
    ComposerDialog,
    RichTextEditor,
    extract_body_fragment,
    images_dir,
    populate_preview,
    register_body_images,
)


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def mail_config(tmp_config, tmp_path):
    """``tmp_config`` with the mailer templates dir pointed at an isolated folder."""
    templates_dir = tmp_path / "templates" / "mail"
    templates_dir.mkdir(parents=True)
    return tmp_config.model_copy(
        update={
            "mailer": tmp_config.mailer.model_copy(
                update={"templates_dir": str(templates_dir)}
            )
        }
    )


def _make_png(path, colour: int = 0x336699, size: tuple[int, int] = (24, 12)) -> None:
    image = QImage(size[0], size[1], QImage.Format.Format_RGB32)
    image.fill(colour)
    image.save(str(path), "PNG")


def _select_all(editor: RichTextEditor):
    cursor = editor.edit.textCursor()
    cursor.select(cursor.SelectionType.Document)
    editor.edit.setTextCursor(cursor)


# --------------------------------------------------------- 純文字 vs. 有格式


def test_plain_multiline_body_round_trips_as_plain_text(qt_app, mail_config):
    editor = RichTextEditor(mail_config)
    editor.set_body("第一行\n第二行\n\n第三段")
    assert editor.to_body_string() == "第一行\n第二行\n\n第三段"


def test_empty_body_round_trips_as_empty_string(qt_app, mail_config):
    editor = RichTextEditor(mail_config)
    editor.set_body("")
    assert editor.to_body_string() == ""


def test_bold_selection_produces_html_without_polluting_style_block(qt_app, mail_config):
    editor = RichTextEditor(mail_config)
    editor.set_body("一般文字")
    _select_all(editor)
    editor._toggle_bold()

    html = editor.to_body_string()
    assert looks_like_html(html)
    assert "<style" not in html
    assert "white-space: pre-wrap" not in html  # QTextEdit 的 <head><style> 內容


def test_toggle_bold_twice_clears_it_again(qt_app, mail_config):
    editor = RichTextEditor(mail_config)
    editor.set_body("一般文字")
    _select_all(editor)
    editor._toggle_bold()
    editor._toggle_bold()
    assert not editor._has_rich_formatting()


# --------------------------------------------------------------------- 圖片


def test_insert_image_produces_relative_cid_ready_src(qt_app, mail_config, tmp_path):
    editor = RichTextEditor(mail_config)
    editor.set_body("")

    source = tmp_path / "logo.png"
    _make_png(source)
    filename = editor._store_image(source)
    editor._place_image(filename)

    html = editor.to_body_string()
    assert f'src="images/{filename}"' in html
    assert (images_dir(mail_config) / filename).exists()


def test_produced_html_feeds_sender_resolve_images_unchanged(qt_app, mail_config, tmp_path):
    """The whole point of switching to QTextEdit: gmail/sender.py needs zero changes."""
    editor = RichTextEditor(mail_config)
    editor.set_body("開頭文字")
    source = tmp_path / "banner.png"
    _make_png(source)
    filename = editor._store_image(source)
    editor._place_image(filename)

    html = editor.to_body_string()
    sender = SmtpSender(mail_config)
    rewritten, images = sender._resolve_images(html)

    assert len(images) == 1
    (data, subtype), = images.values()
    assert subtype == "png"
    assert data == source.read_bytes()
    assert "cid:" in rewritten
    assert "images/" not in rewritten


def test_round_trip_preserves_bold_and_image_on_reload(qt_app, mail_config, tmp_path):
    editor = RichTextEditor(mail_config)
    editor.set_body("一般文字")
    _select_all(editor)
    editor._toggle_bold()

    source = tmp_path / "pic.png"
    _make_png(source)
    filename = editor._store_image(source)
    editor._place_image(filename)

    first_html = editor.to_body_string()

    second = RichTextEditor(mail_config)
    second.set_body(first_html)
    second_html = second.to_body_string()

    assert looks_like_html(second_html)
    assert f"images/{filename}" in second_html


def test_missing_image_file_falls_back_to_placeholder_text(qt_app, mail_config):
    editor = RichTextEditor(mail_config)
    editor.set_body("")
    editor._place_image("does-not-exist.png")
    assert "[圖片：does-not-exist.png]" in editor.to_body_string()


def test_register_body_images_skips_missing_files_without_raising(tmp_path):
    document = QTextDocument()
    register_body_images(document, '<img src="images/missing.png">', tmp_path)  # no raise


# --------------------------------------------------------------- 片段抽取


def test_extract_body_fragment_strips_head_and_style():
    full = (
        "<html><head><style>p { color: red; }</style></head>"
        "<body><p>內容</p></body></html>"
    )
    assert extract_body_fragment(full) == "<p>內容</p>"


def test_extract_body_fragment_returns_input_unchanged_when_no_body_tag():
    assert extract_body_fragment("<p>片段</p>") == "<p>片段</p>"


# --------------------------------------------------------------- 放大編輯視窗


def test_composer_dialog_apply_returns_edited_html(qt_app, mail_config):
    dialog = ComposerDialog(None, "原始內容", mail_config)
    cursor = dialog.editor.edit.textCursor()
    cursor.movePosition(cursor.MoveOperation.End)
    cursor.insertText("追加文字")
    dialog.editor.edit.setTextCursor(cursor)
    dialog.accept()
    assert "追加文字" in dialog.result_body()


def test_composer_dialog_reject_returns_none(qt_app, mail_config):
    dialog = ComposerDialog(None, "原始內容", mail_config)
    dialog.reject()
    assert dialog.result_body() is None


# ------------------------------------------------------------------- 預覽


def test_populate_preview_is_read_only_and_shows_plain_fallback(qt_app, mail_config):
    edit = QTextEdit()
    populate_preview(edit, "純文字內容", mail_config)
    assert edit.isReadOnly()
    assert edit.toPlainText() == "純文字內容"


def test_populate_preview_renders_saved_image(qt_app, mail_config, tmp_path):
    editor = RichTextEditor(mail_config)
    editor.set_body("")
    source = tmp_path / "preview.png"
    _make_png(source)
    filename = editor._store_image(source)
    editor._place_image(filename)
    html = editor.to_body_string()

    edit = QTextEdit()
    populate_preview(edit, html, mail_config)
    assert "￼" in edit.toPlainText()  # QTextEdit 表示內嵌圖片的替代字元
