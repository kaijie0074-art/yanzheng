from __future__ import annotations

import hashlib
import sqlite3
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas

from research_app import documents
from research_app.documents import (
    document_pages,
    import_pdf,
    pdf_bytes,
    search_pages,
    selected_pages,
)
from research_app.storage import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


def make_pdf(*pages: str | None) -> bytes:
    output = BytesIO()
    pdf = canvas.Canvas(output, invariant=1)
    for text in pages:
        if text is not None:
            pdf.drawString(40, 750, text)
        else:
            # 只有图形、没有文本层，模拟扫描页的提取行为。
            pdf.rect(40, 500, 200, 200, fill=1)
        pdf.showPage()
    pdf.save()
    return output.getvalue()


def metadata(store, title="正式来源"):
    return store.add_source(
        title=title,
        authors=["Researcher"],
        year=2025,
        official_url="https://example.test/paper",
        inclusion_reason="人工纳入",
    )


def test_import_persists_original_physical_pages_and_download(store):
    data = make_pdf("first page", "second page")
    source = import_pdf(
        store,
        data,
        title="Paper",
        authors=["A", "B"],
        year=2025,
        official_url="https://example.test/paper",
    )
    assert source["sha256"] == hashlib.sha256(data).hexdigest()
    assert source["page_count"] == 2
    assert source["authors"] == ["A", "B"]
    assert source["access_level"] == "fulltext"
    assert source["bibliography_status"] == "unverified"
    reopened = Store(store.data_dir)
    assert reopened.get_source(source["id"])["title"] == "Paper"
    pages = document_pages(reopened, source["id"])
    assert [page["page_number"] for page in pages] == [1, 2]
    assert "first page" in pages[0]["text"]
    assert "second page" in pages[1]["text"]
    assert all(page["text_status"] == "有可提取文本" for page in pages)
    assert pdf_bytes(reopened, source["id"]) == data
    assert [path.name for path in store.papers_dir.iterdir()] == [
        source["sha256"] + ".pdf"
    ]


def test_duplicate_does_not_change_bibliography_or_create_files(store):
    data = make_pdf("same PDF")
    first = import_pdf(store, data, title="Original")
    second = import_pdf(store, data, title="Different title")
    assert first == second
    assert len(store.list_sources()) == 1
    assert len(store.get_pages(first["id"])) == 1
    assert len(list(store.papers_dir.iterdir())) == 1


def test_attach_preserves_existing_bibliography_and_duplicate_returns_same_source(
    store,
):
    source_id = metadata(store)
    store.set_bibliography(source_id, True)
    before = store.get_source(source_id)
    data = make_pdf("attached text")
    attached = import_pdf(store, data, source_id=source_id, title="Must not replace")
    assert attached["id"] == source_id
    for key in (
        "title",
        "authors",
        "year",
        "official_url",
        "bibliography_status",
        "inclusion_reason",
    ):
        assert attached[key] == before[key]
    assert import_pdf(store, data, source_id=source_id)["id"] == source_id
    another_id = metadata(store, "另一来源")
    assert import_pdf(store, data, source_id=another_id)["id"] == source_id
    assert store.get_source(another_id)["sha256"] is None
    with pytest.raises(ValueError, match="已有PDF"):
        import_pdf(store, make_pdf("different content"), source_id=source_id)
    assert pdf_bytes(store, source_id) == data


def test_missing_target_never_creates_or_deduplicates_silently(store):
    data = make_pdf("existing")
    original = import_pdf(store, data)
    with pytest.raises(ValueError, match="来源不存在"):
        import_pdf(store, data, source_id="not-a-source")
    assert len(store.list_sources()) == 1
    assert pdf_bytes(store, original["id"]) == data


def test_no_text_is_preserved_but_not_claimed_as_read_fulltext(store):
    data = make_pdf(None, None)
    source = import_pdf(store, data)
    assert source["access_level"] == "metadata"
    assert source["text_status"] == "无可提取文本"
    reopened = Store(store.data_dir)
    assert reopened.get_source(source["id"])["access_level"] == "metadata"
    pages = document_pages(reopened, source["id"])
    assert len(pages) == 2
    assert all(
        page["text"] == "" and page["text_status"] == "无可提取文本" for page in pages
    )
    assert pdf_bytes(reopened, source["id"]) == data
    with pytest.raises(ValueError, match="无可提取文本"):
        selected_pages(reopened, [(source["id"], 1), (source["id"], 2)])


def test_mixed_document_keeps_empty_physical_page_without_inserting_label(store):
    source = import_pdf(store, make_pdf("text page", None))
    assert source["access_level"] == "fulltext"
    pages = document_pages(store, source["id"])
    assert pages[1]["text"] == ""
    assert pages[1]["text_status"] == "无可提取文本"
    selected = selected_pages(store, [(source["id"], 1), (source["id"], 2)])
    assert len(selected) == 2
    assert selected[1]["text"] == ""
    with pytest.raises(ValueError, match="无可提取文本"):
        selected_pages(store, [(source["id"], 2)])


def encrypted_pdf():
    writer = PdfWriter()
    writer.append(PdfReader(BytesIO(make_pdf("secret"))))
    writer.encrypt("password")
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.parametrize(
    "bad_data, message",
    [
        (b"", "非空"),
        (b"not a pdf", "不是"),
        (b"%PDF-1.7\ntruncated broken document", "损坏"),
        (b"x" * (20 * 1024 * 1024 + 1), "20MiB"),
    ],
)
def test_invalid_upload_preserves_existing_source_and_files(store, bad_data, message):
    existing = import_pdf(store, make_pdf("keep original"))
    target_id = metadata(store)
    before = store.list_sources()
    files_before = set(store.papers_dir.iterdir())
    with pytest.raises(ValueError, match=message):
        import_pdf(store, bad_data, source_id=target_id)
    assert store.list_sources() == before
    assert set(store.papers_dir.iterdir()) == files_before
    assert store.get_pages(target_id) == []
    assert "keep original" in document_pages(store, existing["id"])[0]["text"]


def test_encrypted_and_zero_page_pdfs_are_rejected_without_persistence(store):
    with pytest.raises(ValueError, match="加密"):
        import_pdf(store, encrypted_pdf())
    output = BytesIO()
    PdfWriter().write(output)
    with pytest.raises(ValueError, match="1至100"):
        import_pdf(store, output.getvalue())
    assert store.list_sources() == []
    assert list(store.papers_dir.iterdir()) == []


def test_page_limit_accepts_100_and_rejects_101(store):
    source = import_pdf(store, make_pdf(*[f"page {i}" for i in range(100)]))
    assert source["page_count"] == 100
    assert store.get_pages(source["id"])[-1]["page_number"] == 100
    with pytest.raises(ValueError, match="1至100"):
        import_pdf(store, make_pdf(*[f"page {i}" for i in range(101)]))
    assert len(store.list_sources()) == 1
    assert len(list(store.papers_dir.iterdir())) == 1


def test_byte_limit_is_inclusive(store, monkeypatch):
    data = make_pdf("boundary")
    monkeypatch.setattr(documents, "MAX_PDF_BYTES", len(data))
    source = import_pdf(store, data)
    assert pdf_bytes(store, source["id"]) == data
    with pytest.raises(ValueError, match="20MiB"):
        import_pdf(store, data + b"\n")


def test_pdf_limit_counts_documents_not_metadata_and_allows_duplicate_at_limit(store):
    for index in range(21):
        metadata(store, f"metadata {index}")
    originals = []
    for index in range(20):
        data = make_pdf(f"document {index}")
        originals.append((data, import_pdf(store, data)))
    with pytest.raises(ValueError, match="20份"):
        import_pdf(store, make_pdf("document over limit"))
    data, source = originals[0]
    assert import_pdf(store, data)["id"] == source["id"]
    assert len(store.list_sources()) == 41
    assert len(list(store.papers_dir.iterdir())) == 20


@pytest.mark.parametrize("attach", [False, True])
def test_database_failure_rolls_back_source_pages_and_new_file(store, attach):
    original = import_pdf(store, make_pdf("keep"))
    target_id = metadata(store) if attach else None
    before = store.list_sources()
    files_before = set(store.papers_dir.iterdir())
    with store.connection() as db:
        db.execute(
            "CREATE TRIGGER reject_second_page BEFORE INSERT ON pages "
            "WHEN NEW.page_number=2 BEGIN SELECT RAISE(ABORT, 'test page failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="test page failure"):
        import_pdf(store, make_pdf("first", "second"), source_id=target_id)
    assert store.list_sources() == before
    assert set(store.papers_dir.iterdir()) == files_before
    assert len(store.rows("SELECT * FROM pages")) == 1
    assert "keep" in store.get_pages(original["id"])[0]["text"]


def test_file_write_failure_does_not_leave_database_records(store, monkeypatch):
    original_open = documents.os.open

    def fail_material_open(path, *args, **kwargs):
        if Path(path).parent == store.papers_dir:
            raise OSError("disk unavailable")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(documents.os, "open", fail_material_open)
    with pytest.raises(OSError, match="disk unavailable"):
        import_pdf(store, make_pdf("cannot save"))
    assert store.list_sources() == []
    assert store.rows("SELECT * FROM pages") == []
    assert list(store.papers_dir.iterdir()) == []


def test_search_normalizes_unicode_and_case_but_not_sql_wildcards(store):
    source = import_pdf(store, make_pdf("page one", "page two", "page three"))
    texts = ["ＡＢＣ Straße 100%", "abc STRASSE _ literal", "unmatched"]
    with store.connection() as db:
        db.executemany(
            "UPDATE pages SET text=? WHERE source_id=? AND page_number=?",
            [(text, source["id"], i) for i, text in enumerate(texts, 1)],
        )
    assert [page["page_number"] for page in search_pages(store, "abc")] == [1, 2]
    assert [page["page_number"] for page in search_pages(store, "STRASSE")] == [1, 2]
    assert [page["page_number"] for page in search_pages(store, "%")] == [1]
    assert [page["page_number"] for page in search_pages(store, "_")] == [2]
    assert search_pages(store, "' OR 1=1 --") == []
    assert search_pages(store, "nothing matches") == []
    assert search_pages(store, "abc")[0]["text"] == texts[0]
    with pytest.raises(ValueError, match="非空关键词"):
        search_pages(store, " \n")


def test_search_groups_documents_and_orders_physical_pages(store):
    one = import_pdf(store, make_pdf("match 1", "match 2"))
    two = import_pdf(store, make_pdf("match 3", "match 4"))
    matches = search_pages(store, "match")
    assert [(p["source_id"], p["page_number"]) for p in matches] == [
        (one["id"], 1),
        (one["id"], 2),
        (two["id"], 1),
        (two["id"], 2),
    ]


def test_selection_returns_full_originals_once_in_requested_order(store):
    source = import_pdf(store, make_pdf("first", "second"))
    sid = source["id"]
    result = selected_pages(store, [(sid, 2), (sid, 1), (sid, 2)])
    assert result == [store.get_pages(sid)[1], store.get_pages(sid)[0]]


@pytest.mark.parametrize("number", [0, -1, 3, True, "1"])
def test_selection_rejects_invalid_or_missing_physical_pages(store, number):
    source = import_pdf(store, make_pdf("one", "two"))
    with pytest.raises(ValueError):
        selected_pages(store, [(source["id"], number)])


def test_selection_rejects_empty_missing_and_metadata_only_sources(store):
    with pytest.raises(ValueError, match="至少选择"):
        selected_pages(store, [])
    with pytest.raises(ValueError, match="不存在"):
        selected_pages(store, [("missing", 1)])
    sid = metadata(store)
    with pytest.raises(ValueError, match="不存在"):
        selected_pages(store, [(sid, 1)])
    with pytest.raises(ValueError, match="尚未导入"):
        pdf_bytes(store, sid)


def test_selection_24000_boundary_never_silently_truncates(store):
    source = import_pdf(store, make_pdf("one", "two"))
    sid = source["id"]
    with store.connection() as db:
        db.execute(
            "UPDATE pages SET text=? WHERE source_id=? AND page_number=?",
            ("x" * 24000, sid, 1),
        )
        db.execute(
            "UPDATE pages SET text=? WHERE source_id=? AND page_number=?", ("y", sid, 2)
        )
    result = selected_pages(store, [(sid, 1)])
    assert result[0]["text"] == "x" * 24000
    with pytest.raises(ValueError, match="24000.*未截断"):
        selected_pages(store, [(sid, 1), (sid, 2)])
    assert store.get_pages(sid)[0]["text"] == "x" * 24000
    assert selected_pages(store, [(sid, 1), (sid, 1)]) == result


def test_title_cannot_control_file_path_and_corrupt_hash_cannot_traverse(store):
    data = make_pdf("safe")
    source = import_pdf(store, data, title="../../outside.pdf")
    assert list(store.papers_dir.iterdir()) == [
        store.papers_dir / (source["sha256"] + ".pdf")
    ]
    assert pdf_bytes(store, source["id"]) == data
    with store.connection() as db:
        db.execute(
            "UPDATE sources SET sha256=? WHERE id=?", ("../../outside", source["id"])
        )
    with pytest.raises(ValueError, match="哈希无效"):
        pdf_bytes(store, source["id"])


def test_download_rejects_tampering_and_symlinks(store, tmp_path):
    source = import_pdf(store, make_pdf("original"))
    path = store.papers_dir / (source["sha256"] + ".pdf")
    path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="哈希不一致"):
        pdf_bytes(store, source["id"])
    path.unlink()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(make_pdf("outside"))
    path.symlink_to(outside)
    with pytest.raises(ValueError, match="安全读取"):
        pdf_bytes(store, source["id"])
    assert outside.read_bytes() == make_pdf("outside")
