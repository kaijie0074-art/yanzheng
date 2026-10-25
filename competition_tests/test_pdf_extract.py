from __future__ import annotations

import subprocess
import sys
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

import psutil
import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    EncodedStreamObject,
    NameObject,
    NumberObject,
    RectangleObject,
)
from reportlab.pdfgen import canvas

from research_app import pdf_extract
from research_app.pdf_extract import PDFExtractionError, extract_pdf


def make_pdf(*pages: str | None) -> bytes:
    output = BytesIO()
    pdf = canvas.Canvas(output, invariant=1)
    for text in pages:
        if text is None:
            pdf.rect(40, 500, 200, 200, fill=1)
        else:
            pdf.drawString(40, 750, text)
        pdf.showPage()
    pdf.save()
    return output.getvalue()


def compressed_pdf(*stream_sizes: int) -> bytes:
    writer = PdfWriter()
    for size in stream_sizes:
        page = writer.add_page(PdfReader(BytesIO(make_pdf("font resource"))).pages[0])
        stream = EncodedStreamObject()
        # 一个长注释使测试针对解压量，不依赖数百万绘图指令的执行速度。
        stream._data = zlib.compress(b"%" + b"x" * (size - 2) + b"\n")
        stream[NameObject("/Filter")] = NameObject("/FlateDecode")
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def nested_form_pdf(
    form: DecodedStreamObject | EncodedStreamObject | None = None, *, depth: int = 3
) -> bytes:
    """真实正文引用Form链；默认叶流长度真实超限，不伪造/截断Length或xref。"""
    writer = PdfWriter()
    page = writer.add_page(
        PdfReader(BytesIO(make_pdf("normal body retained"))).pages[0]
    )
    original_content = page.get_contents().get_data()
    fonts = page["/Resources"]["/Font"]
    if form is None:
        form = DecodedStreamObject()
        form.set_data(b"%" + b"x" * pdf_extract.MAX_STREAM_BYTES + b"\n")
    reference = None
    for level in range(depth):
        if level:
            form = DecodedStreamObject()
            form.set_data(b"/Nested Do\n")
        resources = DictionaryObject({NameObject("/Font"): fonts})
        if reference is not None:
            resources[NameObject("/XObject")] = DictionaryObject(
                {NameObject("/Nested"): reference}
            )
        form[NameObject("/Type")] = NameObject("/XObject")
        form[NameObject("/Subtype")] = NameObject("/Form")
        form[NameObject("/BBox")] = RectangleObject([0, 0, 100, 100])
        form[NameObject("/Resources")] = resources
        reference = writer._add_object(form)
    page["/Resources"][NameObject("/XObject")] = DictionaryObject(
        {NameObject("/Nested"): reference}
    )
    content = DecodedStreamObject()
    content.set_data(original_content + b"\n/Nested Do\n")
    page[NameObject("/Contents")] = writer._add_object(content)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def worker_script(monkeypatch, script: str) -> None:
    monkeypatch.setattr(
        pdf_extract,
        "_worker_command",
        lambda _pages: [sys.executable, "-I", "-c", script],
    )


def configured_worker(monkeypatch, **settings) -> None:
    # 测试配置只进独立子进程；生产入口不接受用户提供的Python代码或资源阈值。
    module = str(Path(pdf_extract.__file__).resolve())
    assignments = ";".join(f"m.{key}={value!r}" for key, value in settings.items())
    prefix = (
        "import importlib.util;"
        f"s=importlib.util.spec_from_file_location('pdf_worker',{module!r});"
        "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
        f"{assignments};"
    )
    monkeypatch.setattr(
        pdf_extract,
        "_worker_command",
        lambda pages: [sys.executable, "-I", "-c", prefix + f"m._worker_main({pages})"],
    )


@pytest.fixture
def children(monkeypatch):
    original = subprocess.Popen
    processes = []
    lock = threading.Lock()

    def record(*args, **kwargs):
        process = original(*args, **kwargs)
        with lock:
            processes.append(process)
        return process

    monkeypatch.setattr(pdf_extract.subprocess, "Popen", record)
    yield processes
    # 即使断言失败也不留下测试子进程。
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.wait()


def assert_reaped(processes):
    assert processes
    assert all(process.returncode is not None for process in processes)
    assert all(not psutil.pid_exists(process.pid) for process in processes)


def test_real_pdf_preserves_physical_page_order_and_empty_pages(children):
    pages = extract_pdf(make_pdf("first page", None, "third page"))
    assert len(pages) == 3
    assert "first page" in pages[0]
    assert pages[1] == ""
    assert "third page" in pages[2]
    assert_reaped(children)


def test_scan_only_pdf_returns_empty_text_not_fake_ocr():
    assert extract_pdf(make_pdf(None, None)) == ["", ""]


@pytest.mark.parametrize(
    "data, message",
    [
        (b"", "非空"),
        (b"plain text", "不是"),
        (b"%PDF-1.7\nprivate-path-secret broken", "损坏"),
        (b"%PDF-" + b"x" * (20 * 1024 * 1024), "20MiB"),
    ],
)
def test_invalid_pdf_has_safe_error(data, message):
    with pytest.raises(PDFExtractionError, match=message) as error:
        extract_pdf(data)
    assert "private-path-secret" not in str(error.value)


def test_encrypted_pdf_is_rejected_even_if_empty_password():
    writer = PdfWriter()
    writer.append(PdfReader(BytesIO(make_pdf("encrypted text"))))
    writer.encrypt("")
    output = BytesIO()
    writer.write(output)
    with pytest.raises(PDFExtractionError, match="加密"):
        extract_pdf(output.getvalue())


def test_page_limit_accepts_boundary_and_rejects_extra_page():
    assert len(extract_pdf(make_pdf(*([None] * 100)))) == 100
    with pytest.raises(PDFExtractionError, match="1至100页"):
        extract_pdf(make_pdf(*([None] * 101)))
    with pytest.raises(PDFExtractionError, match="1至1页"):
        extract_pdf(make_pdf("one", "two"), max_pages=1)


def test_zero_page_pdf_is_rejected():
    output = BytesIO()
    PdfWriter().write(output)
    with pytest.raises(PDFExtractionError, match="1至100页"):
        extract_pdf(output.getvalue())


def test_custom_byte_limit_is_enforced_before_spawning(children):
    data = make_pdf("bounded")
    with pytest.raises(PDFExtractionError, match="大小"):
        extract_pdf(data, max_bytes=len(data) - 1)
    assert children == []
    assert "bounded" in extract_pdf(data, max_bytes=len(data))[0]


def test_single_high_compression_stream_is_rejected(children):
    data = compressed_pdf(pdf_extract.MAX_STREAM_BYTES + 1024)
    assert len(data) < 20_000
    with pytest.raises(PDFExtractionError, match="解压资源超限"):
        extract_pdf(data)
    assert_reaped(children)


def test_under_limit_compressed_stream_is_really_parsed():
    assert extract_pdf(compressed_pdf(1024 * 1024)) == [""]


def test_many_individually_legal_streams_exhaust_document_budget(children):
    data = compressed_pdf(*([7 * 1024 * 1024] * 5))
    assert len(data) < 100_000
    with pytest.raises(PDFExtractionError, match="解压资源超限"):
        extract_pdf(data)
    assert_reaped(children)


def test_cumulative_budget_counts_intermediate_filter_output(monkeypatch):
    from pypdf.generic import ArrayObject

    configured_worker(monkeypatch, MAX_STREAM_BYTES=4096, MAX_DECODED_BYTES=12000)
    writer = PdfWriter()
    # 每个流低于原生单流工作预算；最终输出总量也不足12KiB。
    # 只有累计计入中间滤镜输出时，整份文档才会超限。
    for _ in range(4):
        page = writer.add_page(PdfReader(BytesIO(make_pdf("font resource"))).pages[0])
        stream = EncodedStreamObject()
        content = b"%" + bytes(range(32, 127)) * 21 + b"\n"
        compressed = zlib.compress(content, level=0)
        assert len(content) * 4 < 12000
        stream._data = compressed.hex().encode() + b">"
        stream[NameObject("/Filter")] = ArrayObject(
            [NameObject("/ASCIIHexDecode"), NameObject("/FlateDecode")]
        )
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    with pytest.raises(PDFExtractionError, match="解压资源超限"):
        extract_pdf(output.getvalue())


def test_text_budget_is_cumulative_and_does_not_return_partial_pages(monkeypatch):
    configured_worker(monkeypatch, MAX_TEXT_CHARACTERS=100)
    with pytest.raises(PDFExtractionError, match="全文文本"):
        extract_pdf(make_pdf("a" * 60, "b" * 60))


def test_parent_pypdf_remains_usable_after_rejected_bomb():
    with pytest.raises(PDFExtractionError):
        extract_pdf(compressed_pdf(pdf_extract.MAX_STREAM_BYTES + 1))
    reader = PdfReader(BytesIO(make_pdf("parent still parses")))
    assert "parent still parses" in reader.pages[0].extract_text()


@pytest.mark.parametrize("depth", [1, 3])
def test_declared_form_limit_rejects_whole_pdf_without_mutating_parent(depth, children):
    from pypdf.errors import LimitReachedError

    original_init = LimitReachedError.__init__
    data = nested_form_pdf(depth=depth)
    assert len(data) < 20 * 1024 * 1024
    # 父进程默认限额更宽，严格读取能确认这是可解析的完整PDF而非损坏文件。
    assert (
        "normal body retained"
        in PdfReader(BytesIO(data), strict=True).pages[0].extract_text()
    )
    with pytest.raises(PDFExtractionError, match="解压资源超限"):
        extract_pdf(data)
    assert LimitReachedError.__init__ is original_init
    assert isinstance(LimitReachedError("parent limit"), Exception)
    assert (
        "parent still parses"
        in PdfReader(BytesIO(make_pdf("parent still parses")), strict=True)
        .pages[0]
        .extract_text()
    )
    assert_reaped(children)


def test_under_limit_nested_form_preserves_body_and_form_text():
    form = DecodedStreamObject()
    form.set_data(b"BT /F1 12 Tf 10 10 Td (nested form retained) Tj ET\n")
    pages = extract_pdf(nested_form_pdf(form))
    assert len(pages) == 1
    assert "normal body retained" in pages[0]
    assert "nested form retained" in pages[0]


@pytest.mark.parametrize(
    "limit", ["decoded_output", "filter_count", "array_elements", "flate_row"]
)
def test_nested_native_limits_never_return_normal_body_only(limit, children):
    from pypdf import Configuration

    configuration = Configuration()
    if limit == "array_elements":
        form = DecodedStreamObject()
        form.set_data(
            b"[" + b"0 " * (configuration.array_maximum_element_count + 1) + b"] TJ\n"
        )
    else:
        form = EncodedStreamObject()
        form[NameObject("/Filter")] = NameObject("/FlateDecode")
        if limit == "decoded_output":
            form._data = zlib.compress(
                b"%" + b"x" * pdf_extract.MAX_STREAM_BYTES + b"\n"
            )
        elif limit == "filter_count":
            form._data = b"61>"
            form[NameObject("/Filter")] = ArrayObject(
                [NameObject("/ASCIIHexDecode")]
                * (configuration.stream_filters_maximum_length + 1)
            )
        else:
            form._data = zlib.compress(b"\x00")
            form[NameObject("/DecodeParms")] = DictionaryObject(
                {
                    NameObject("/Predictor"): NumberObject(2),
                    NameObject("/Columns"): NumberObject(
                        pdf_extract.MAX_STREAM_BYTES + 1
                    ),
                }
            )
    data = nested_form_pdf(form)
    assert len(data) < 20 * 1024 * 1024
    with pytest.raises(PDFExtractionError, match="解压资源超限"):
        extract_pdf(data)
    assert_reaped(children)


@pytest.mark.parametrize("attach", [False, True])
def test_real_import_rejects_nested_declared_limit_without_partial_persistence(
    tmp_path, attach
):
    from research_app.documents import import_pdf
    from research_app.storage import Store

    store = Store(tmp_path / "data")
    source_id = (
        store.add_source(
            title="Existing metadata",
            authors=["Researcher"],
            year=2025,
            official_url="https://example.test/paper",
        )
        if attach
        else None
    )
    before = store.list_sources()
    with pytest.raises(PDFExtractionError, match="解压资源超限"):
        import_pdf(store, nested_form_pdf(), source_id=source_id, title="Rejected PDF")
    reopened = Store(store.data_dir)
    assert reopened.list_sources() == before
    assert reopened.rows("SELECT * FROM pages") == []
    assert list(reopened.papers_dir.iterdir()) == []


def test_timeout_kills_only_owned_child_and_allows_next_import(monkeypatch, children):
    original_command = pdf_extract._worker_command
    unrelated = subprocess.Popen(
        [sys.executable, "-I", "-c", "import time;time.sleep(60)"]
    )
    try:
        worker_script(monkeypatch, "import time;time.sleep(60)")
        monkeypatch.setattr(pdf_extract, "MAX_SECONDS", 0.3)
        start = time.monotonic()
        with pytest.raises(PDFExtractionError, match="超时"):
            extract_pdf(make_pdf("slow"))
        assert time.monotonic() - start < 5
        assert unrelated.poll() is None
        assert_reaped(children[1:])
        monkeypatch.setattr(pdf_extract, "_worker_command", original_command)
        monkeypatch.setattr(pdf_extract, "MAX_SECONDS", 30)
        assert "recovered" in extract_pdf(make_pdf("recovered"))[0]
    finally:
        unrelated.kill()
        unrelated.wait()


def test_rss_limit_stops_actual_allocation_and_reaps_child(monkeypatch, children):
    worker_script(
        monkeypatch,
        "import time;buffer=bytearray(128*1024*1024);time.sleep(60)",
    )
    monkeypatch.setattr(pdf_extract, "MAX_RSS_BYTES", 40 * 1024 * 1024)
    monkeypatch.setattr(pdf_extract, "MAX_SECONDS", 5)
    with pytest.raises(PDFExtractionError, match="内存超限"):
        extract_pdf(make_pdf("memory"))
    assert_reaped(children)


def test_cpu_limit_terminates_busy_child(monkeypatch, children):
    module = str(Path(pdf_extract.__file__).resolve())
    worker_script(
        monkeypatch,
        "import importlib.util;"
        f"s=importlib.util.spec_from_file_location('pdf_worker',{module!r});"
        "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
        "m.MAX_CPU_SECONDS=1;m._set_process_limits();\nwhile True: pass",
    )
    monkeypatch.setattr(pdf_extract, "MAX_SECONDS", 5)
    with pytest.raises(PDFExtractionError, match="资源超限"):
        extract_pdf(make_pdf("cpu"))
    assert_reaped(children)


@pytest.mark.parametrize(
    "script",
    [
        "import sys;sys.stderr.write('private-secret');sys.exit(17)",
        "print('private-secret malformed response')",
        'print(\'{"error":"private-secret"}\')',
        "print('{\"pages\":[42]}')",
    ],
)
def test_abnormal_child_output_is_sanitized_and_reaped(monkeypatch, children, script):
    worker_script(monkeypatch, script)
    with pytest.raises(PDFExtractionError, match="资源超限") as error:
        extract_pdf(make_pdf("invalid child output"))
    assert "private-secret" not in str(error.value)
    assert_reaped(children)


def test_stdout_flood_is_bounded_and_reaped(monkeypatch, children):
    worker_script(
        monkeypatch,
        "import os,time;os.write(1,b'x'*65536);time.sleep(60)",
    )
    monkeypatch.setattr(pdf_extract, "MAX_OUTPUT_BYTES", 4096)
    monkeypatch.setattr(pdf_extract, "MAX_SECONDS", 5)
    with pytest.raises(PDFExtractionError, match="资源超限"):
        extract_pdf(make_pdf("stdout"))
    assert_reaped(children)


def test_worker_does_not_inherit_api_credentials(monkeypatch):
    monkeypatch.setenv("RESEARCH_API_KEY", "private-secret")
    worker_script(
        monkeypatch,
        "import os,json;print(json.dumps({'pages':[str('RESEARCH_API_KEY' in os.environ)]}))",
    )
    assert extract_pdf(make_pdf("environment")) == ["False"]


def test_at_most_two_workers_and_capacity_is_released(monkeypatch, children):
    worker_script(
        monkeypatch,
        'import time;time.sleep(0.3);print(\'{"pages":["completed"]}\')',
    )
    data = make_pdf("parallel")
    max_active = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(extract_pdf, data) for _ in range(4)]
        while not all(future.done() for future in futures):
            max_active = max(max_active, sum(p.poll() is None for p in list(children)))
            time.sleep(0.005)
        assert [future.result() for future in futures] == [["completed"]] * 4
    assert max_active == 2
    assert len(children) == 4
    assert_reaped(children)


def test_waiting_for_capacity_is_included_in_deadline(monkeypatch):
    monkeypatch.setattr(pdf_extract, "MAX_SECONDS", 0.05)
    slots = threading.BoundedSemaphore(2)
    monkeypatch.setattr(pdf_extract, "_SLOTS", slots)
    slots.acquire()
    slots.acquire()
    try:
        with pytest.raises(PDFExtractionError, match="繁忙或等待超时"):
            extract_pdf(make_pdf("queued"))
    finally:
        slots.release()
        slots.release()
