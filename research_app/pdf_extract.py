"""在一次性子进程中提取PDF；资源限制不是任意代码的操作系统沙箱。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import psutil

MAX_SECONDS = 30.0
MAX_CPU_SECONDS = 20
MAX_RSS_BYTES = 512 * 1024 * 1024
MAX_ADDRESS_BYTES = 1024 * 1024 * 1024
MAX_STREAM_BYTES = 8 * 1024 * 1024
MAX_DECODED_BYTES = 32 * 1024 * 1024
MAX_TEXT_CHARACTERS = 2_000_000
MAX_OUTPUT_BYTES = 12 * 1024 * 1024
_POLL_SECONDS = 0.02
_SLOTS = threading.BoundedSemaphore(2)
_ERRORS = {
    "invalid": "PDF损坏或原文无法解析，未保存本次材料",
    "encrypted": "不支持加密PDF，请合法取得未加密版本后导入",
    "pages": "PDF页数超出允许范围，未保存本次材料",
    "decoded": "PDF解压资源超限，未保存本次材料",
    "text": "PDF全文文本超过200万字符，未保存本次材料",
    "resource": "PDF解析资源超限或子进程异常，未保存本次材料",
}


class PDFExtractionError(ValueError):
    """可直接向使用者展示、不包含底层异常或文档内容的错误。"""


class _ResourceLimit(BaseException):
    # pypdf会捕获部分Exception并跳过XObject；预算耗尽必须停止整个文档，不能返回残文。
    def __init__(self, code: str):
        self.code = code


def _worker_command(max_pages: int) -> list[str]:
    return [
        sys.executable,
        "-I",
        str(Path(__file__).resolve()),
        "--worker",
        str(max_pages),
    ]


def _read_result(output, max_pages: int) -> list[str]:
    output.seek(0)
    raw = output.read(MAX_OUTPUT_BYTES + 1)
    if len(raw) > MAX_OUTPUT_BYTES:
        raise PDFExtractionError(_ERRORS["resource"])
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError):
        raise PDFExtractionError(_ERRORS["resource"]) from None
    if not isinstance(result, dict):
        raise PDFExtractionError(_ERRORS["resource"])
    if "error" in result:
        code = result["error"]
        message = (
            _ERRORS.get(code, _ERRORS["resource"])
            if isinstance(code, str)
            else _ERRORS["resource"]
        )
        if code == "pages":
            message = f"PDF页数须为1至{max_pages}页"
        raise PDFExtractionError(message)
    pages = result.get("pages")
    if (
        set(result) != {"pages"}
        or not isinstance(pages, list)
        or not 1 <= len(pages) <= max_pages
        or not all(isinstance(text, str) for text in pages)
        or sum(map(len, pages)) > MAX_TEXT_CHARACTERS
    ):
        raise PDFExtractionError(_ERRORS["resource"])
    return pages


def extract_pdf(
    data: bytes, *, max_pages: int = 100, max_bytes: int = 20 * 1024 * 1024
) -> list[str]:
    """保留空文本页和物理页序；仅返回完整成功结果。每个API进程最多两个解析子进程。"""
    if not isinstance(data, bytes) or not data:
        raise PDFExtractionError("请选择非空PDF文件")
    if type(max_pages) is not int or not 1 <= max_pages <= 100:
        raise PDFExtractionError("PDF页数上限须为1至100")
    if type(max_bytes) is not int or not 1 <= max_bytes <= 20 * 1024 * 1024:
        raise PDFExtractionError("PDF大小上限须在20MiB以内")
    if len(data) > max_bytes:
        raise PDFExtractionError("PDF超过允许大小（最多20MiB）")
    if not data.startswith(b"%PDF-"):
        raise PDFExtractionError("文件不是可识别的PDF")
    deadline = time.monotonic() + MAX_SECONDS
    if not _SLOTS.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise PDFExtractionError("PDF解析繁忙或等待超时，请稍后重试")
    process = None
    try:
        # 临时文件避免stdin/stdout管道互相阻塞；文件随本次调用关闭，不保存上传原件。
        with tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as output:
            source.write(data)
            source.seek(0)
            process = subprocess.Popen(
                _worker_command(max_pages),
                stdin=source,
                stdout=output,
                stderr=subprocess.DEVNULL,
                env={"PATH": os.defpath, "LANG": "C.UTF-8"},
                close_fds=True,
                start_new_session=True,
            )
            monitored = psutil.Process(process.pid)
            while True:
                if time.monotonic() >= deadline:
                    raise PDFExtractionError("PDF解析超时，未保存本次材料")
                if os.fstat(output.fileno()).st_size > MAX_OUTPUT_BYTES:
                    raise PDFExtractionError(_ERRORS["resource"])
                try:
                    if monitored.memory_info().rss > MAX_RSS_BYTES:
                        raise PDFExtractionError("PDF解析内存超限，未保存本次材料")
                except psutil.NoSuchProcess:
                    pass
                status = process.poll()
                if status is not None:
                    if status != 0:
                        raise PDFExtractionError(_ERRORS["resource"])
                    return _read_result(output, max_pages)
                time.sleep(min(_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
    except (OSError, psutil.Error):
        raise PDFExtractionError(_ERRORS["resource"]) from None
    finally:
        if process is not None:
            # 只回收此Popen拥有的PID，绝不扫描/终止其他PDF或应用进程。
            if process.poll() is None:
                process.kill()
            process.wait()
        _SLOTS.release()


def _set_process_limits() -> None:
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (MAX_CPU_SECONDS, MAX_CPU_SECONDS + 1))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    # macOS共享虚拟映射很大，不能用低RLIMIT_AS冒充RSS限制。
    if sys.platform.startswith("linux"):
        resource.setrlimit(resource.RLIMIT_AS, (MAX_ADDRESS_BYTES, MAX_ADDRESS_BYTES))


def _install_decode_budget() -> None:
    """仅在一次性worker安装；原生限额立即终止，逐层滤镜累计实际输出。"""
    from pypdf import apply_configuration, filters
    from pypdf.errors import LimitReachedError
    from pypdf.generic import StreamObject

    def terminate_native_limit(_self, *_args, **_kwargs):
        raise _ResourceLimit("decoded") from None

    # 修改原异常类的构造入口，而非替换errors模块的名称：pypdf各模块已经
    # 导入的别名及后续导入仍指向同一类。声明流、解析预算、字体和滤镜等
    # 所有原生限额在构造异常时即终止，早于XObject等位置的except Exception。
    # 不按日志/异常文案判断，也不需要逐个包装可能被库内吞错的对象边界。
    LimitReachedError.__init__ = terminate_native_limit

    remaining = MAX_DECODED_BYTES

    def bounded(decoder):
        def decode(*args, **kwargs):
            nonlocal remaining
            allowance = min(MAX_STREAM_BYTES, remaining)
            if allowance <= 0:
                raise _ResourceLimit("decoded")
            with apply_configuration(
                zlib_maximum_output_length=allowance,
                lzw_maximum_output_length=allowance,
                run_length_maximum_output_length=allowance,
                brotli_maximum_output_length=allowance,
                image_maximum_buffer_size=allowance,
            ):
                result = decoder(*args, **kwargs)
            if len(result) > allowance:
                raise _ResourceLimit("decoded")
            remaining -= len(result)
            return result

        return decode

    for name in (
        "ASCIIHexDecode",
        "ASCII85Decode",
        "LZWDecode",
        "FlateDecode",
        "RunLengthDecode",
        "CCITTFaxDecode",
        "DCTDecode",
        "JPXDecode",
        "BrotliDecode",
    ):
        decoder = getattr(filters, name)
        decoder.decode = staticmethod(bounded(decoder.decode))

    # 禁止滤镜调用外部jbig2dec，避免派生未被监控的孙进程。
    def reject_jbig2(*args, **kwargs):
        raise _ResourceLimit("decoded")

    filters.JBIG2Decode.decode = staticmethod(reject_jbig2)
    get_data = StreamObject.get_data

    def bounded_raw_stream(stream):
        result = get_data(stream)
        if len(result) > MAX_STREAM_BYTES:
            raise _ResourceLimit("decoded")
        return result

    StreamObject.get_data = bounded_raw_stream


def _extract_in_worker(data: bytes, max_pages: int) -> list[str]:
    from io import BytesIO

    from pypdf import PdfReader, apply_configuration

    _install_decode_budget()
    with apply_configuration(
        maximum_declared_stream_length=MAX_STREAM_BYTES,
        array_based_stream_maximum_output_length=MAX_STREAM_BYTES,
        image_maximum_buffer_size=MAX_STREAM_BYTES,
        flate_maximum_row_length=MAX_STREAM_BYTES,
        stream_decoding_work_maximum_length=MAX_DECODED_BYTES,
        page_tree_maximum_entries=1000,
        jbig2dec_binary=None,
    ):
        reader = PdfReader(BytesIO(data), strict=True)
        if reader.is_encrypted:
            raise _ResourceLimit("encrypted")
        if not 1 <= len(reader.pages) <= max_pages:
            raise _ResourceLimit("pages")
        texts = []
        characters = 0
        visited_characters = 0

        def visit_text(text, *_args):
            nonlocal visited_characters
            visited_characters += len(text)
            if visited_characters > MAX_TEXT_CHARACTERS:
                raise _ResourceLimit("text")

        for page in reader.pages:
            text = page.extract_text(visitor_text=visit_text) or ""
            characters += len(text)
            if characters > MAX_TEXT_CHARACTERS:
                raise _ResourceLimit("text")
            texts.append(text)
        return texts


def _worker_main(max_pages: int) -> None:
    import logging
    import warnings

    logging.disable(logging.CRITICAL)
    warnings.simplefilter("ignore")
    try:
        _set_process_limits()
        data = sys.stdin.buffer.read(20 * 1024 * 1024 + 1)
        if len(data) > 20 * 1024 * 1024 or not data.startswith(b"%PDF-"):
            raise _ResourceLimit("invalid")
        result = {"pages": _extract_in_worker(data, max_pages)}
    except _ResourceLimit as exc:
        result = {"error": exc.code}
    except MemoryError:
        result = {"error": "resource"}
    except Exception:
        result = {"error": "invalid"}
    encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_OUTPUT_BYTES:
        encoded = b'{"error":"resource"}'
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--worker":
        raise SystemExit(2)
    _worker_main(int(sys.argv[2]))
