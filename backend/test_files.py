"""Tests for file upload persistence and text extraction."""

import io

import pytest

from backend import db, files


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", data)
    monkeypatch.setattr(db, "DB_PATH", data / "app.db")
    monkeypatch.setattr(db, "UPLOADS_DIR", data / "uploads")
    db.init_db()
    yield


def test_is_allowed():
    assert files.is_allowed("report.pdf")
    assert files.is_allowed("data.XLSX")
    assert not files.is_allowed("archive.zip")


# --- filename sanitisation -------------------------------------------------
# The multipart filename is attacker-controlled; save_upload turns it into a
# filesystem path, so a traversal here escaped the vestlus's upload folder.

def test_safe_filename_strips_directory_components():
    assert files.safe_filename("../../../etc/passwd") == "passwd"
    assert files.safe_filename("/absolute/path/report.pdf") == "report.pdf"
    assert files.safe_filename("..\\..\\windows\\evil.txt") == "evil.txt"
    assert files.safe_filename("plain.txt") == "plain.txt"


@pytest.mark.parametrize("bad", ["", "   ", ".", "..", "../", "/", "foo\x00.txt"])
def test_safe_filename_rejects_unusable_names(bad):
    with pytest.raises(ValueError):
        files.safe_filename(bad)


def test_upload_cannot_escape_the_vestlus_directory():
    vid = db.create_vestlus()
    result = files.save_upload(vid, "../../escaped.txt", b"payload")

    assert result["name"] == "escaped.txt"
    written = db.UPLOADS_DIR / vid / "escaped.txt"
    assert written.read_bytes() == b"payload"
    # Nothing anywhere above the per-vestlus folder.
    assert not (db.UPLOADS_DIR.parent / "escaped.txt").exists()
    assert not (db.UPLOADS_DIR / "escaped.txt").exists()


def test_extract_txt_and_csv():
    assert "hello" in files.extract_text("note.txt", b"hello world")
    csv_text = files.extract_text("d.csv", b"a,b\n1,2")
    assert "a, b" in csv_text
    assert "1, 2" in csv_text


def test_extract_xlsx_roundtrip():
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Nimi", "Summa"])
    ws.append(["Projekt", 1000])
    buf = io.BytesIO()
    wb.save(buf)

    text = files.extract_text("book.xlsx", buf.getvalue())
    assert "Nimi" in text
    assert "1000" in text


def test_save_upload_ok_status():
    vid = db.create_vestlus()
    result = files.save_upload(vid, "note.txt", b"company context")
    assert result["status"] == "ok"

    stored = db.get_extracted_texts(vid)
    assert len(stored) == 1
    assert "company context" in stored[0]["extracted_text"]


def test_save_upload_extract_failed_still_stored():
    vid = db.create_vestlus()
    # A .pdf with garbage bytes fails extraction but must still be recorded.
    result = files.save_upload(vid, "broken.pdf", b"not a real pdf")
    assert result["status"] == "extract_failed"

    all_files = db.list_files(vid)
    assert len(all_files) == 1
    assert all_files[0]["status"] == "extract_failed"


def test_delete_file_removes_row_and_disk():
    vid = db.create_vestlus()
    result = files.save_upload(vid, "note.txt", b"bytes")
    path = db.get_file(vid, result["id"])["path"]

    import os

    assert os.path.exists(path)
    returned = db.delete_file(vid, result["id"])
    assert returned == path
    assert db.list_files(vid) == []


def test_text_char_cap_keeps_tail():
    cap = files.MAX_STORED_TEXT_CHARS
    text = files.extract_text("big.txt", ("X" * (cap + 500) + "END").encode("utf-8"))
    assert len(text) <= cap
    assert text.endswith("END")  # tail kept, front trimmed


def test_build_context_text_joins_files_in_order():
    vid = db.create_vestlus()
    files.save_upload(vid, "a.txt", b"first file")
    files.save_upload(vid, "b.txt", b"second file")

    context = files.build_context_text(vid)
    assert "first file" in context
    assert "second file" in context
    assert context.index("first file") < context.index("second file")


def test_build_context_text_empty_when_no_files():
    vid = db.create_vestlus()
    assert files.build_context_text(vid) == ""


def test_build_context_text_trims_combined_front(monkeypatch):
    monkeypatch.setattr(files, "MAX_CONTEXT_CHARS", 50)
    vid = db.create_vestlus()
    files.save_upload(vid, "a.txt", b"X" * 100)
    files.save_upload(vid, "b.txt", b"TAIL")

    context = files.build_context_text(vid)
    assert len(context) <= 50
    assert context.endswith("TAIL")
