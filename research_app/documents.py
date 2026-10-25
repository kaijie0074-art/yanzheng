"""本地PDF材料与物理页原文；不下载URL、不执行论文内容中的指令。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

from .models import now
from .pdf_extract import extract_pdf
from .storage import Store, encoded, uid

MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_PAGES = 100
MAX_DOCUMENTS = 20
MAX_SELECTED_CHARACTERS = 24000


class DocumentConflict(ValueError):
    """原文解析期间研究或来源归属变化，不能附着旧快照。"""


def _check_import_context(
    db, source_id, expected_revision, expected_source, expected_details
) -> None:
    if expected_revision is not None:
        brief = db.execute(
            "SELECT revision FROM briefs ORDER BY revision DESC LIMIT 1"
        ).fetchone()
        if brief is None or brief["revision"] != expected_revision:
            raise DocumentConflict("PDF解析期间研究问题已变化，未采用旧版本原文")
    if expected_source is not None:
        row = db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        current = dict(row) if row is not None else None
        if current is not None:
            current["authors"] = json.loads(current["authors"])
        if current != expected_source:
            raise DocumentConflict("PDF解析期间来源书目或状态已变化，未附着旧论文")
    if expected_details is not None:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_details'"
        ).fetchone()
        row = (
            db.execute(
                "SELECT metadata_json FROM paper_details WHERE source_id=?",
                (source_id,),
            ).fetchone()
            if exists
            else None
        )
        current = json.loads(row["metadata_json"]) if row else {}
        if current != expected_details:
            raise DocumentConflict("PDF解析期间摘要或开放全文归属已变化，未采用旧快照")


def _pdf_path(store: Store, digest: str) -> Path:
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("PDF哈希无效，无法读取原文件")
    return store.papers_dir / f"{digest}.pdf"


def _read_pdf_file(store: Store, digest: str) -> bytes:
    path = _pdf_path(store, digest)
    try:
        # 不跟随被替换为符号链接的材料文件，也不接受数据库中的路径值。
        if any(parent.is_symlink() for parent in path.parents):
            raise ValueError("原PDF所在目录不允许符号链接，请检查材料库")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                raise ValueError("原PDF必须是普通文件，请检查材料库")
            data = file.read(MAX_PDF_BYTES + 1)
    except OSError as exc:
        raise ValueError("原PDF文件缺失或无法安全读取，请重新检查材料库") from exc
    if len(data) > MAX_PDF_BYTES or hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("原PDF与记录哈希不一致，不能作为已核验材料下载")
    return data


def pdf_bytes(store: Store, source_id: str) -> bytes:
    """仅按数据库中的内容哈希读取原PDF，上传标题从不参与路径。"""
    source = store.get_source(source_id)
    if not source["sha256"]:
        raise ValueError("该来源尚未导入PDF")
    return _read_pdf_file(store, source["sha256"])


def _source_result(db, source_id: str) -> dict:
    source = dict(
        db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    )
    source["authors"] = json.loads(source["authors"])
    texts = db.execute("SELECT text FROM pages WHERE source_id=?", (source_id,))
    source["text_status"] = (
        "有可提取文本" if any(row["text"].strip() for row in texts) else "无可提取文本"
    )
    return source


def import_pdf(
    store: Store,
    data: bytes,
    *,
    source_id: str | None = None,
    title: str | None = None,
    authors: list[str] | None = None,
    year: int | None = None,
    official_url: str | None = None,
    expected_revision: int | None = None,
    expected_source: dict | None = None,
    expected_details: dict | None = None,
) -> dict:
    """原子导入文档；同内容返回既有来源，已有PDF不允许被替换。"""
    if not isinstance(data, bytes) or not data:
        raise ValueError("请选择非空PDF文件")
    if len(data) > MAX_PDF_BYTES:
        raise ValueError("每个PDF最多20MiB")
    with store.connection() as db:
        _check_import_context(
            db, source_id, expected_revision, expected_source, expected_details
        )
    digest = hashlib.sha256(data).hexdigest()
    # 去重前也检查指定来源，避免用错误source_id悄悄替换既有材料。
    if source_id is not None:
        existing = store.get_source(source_id)
        if existing["sha256"] and existing["sha256"] != digest:
            raise ValueError("该来源已有PDF，不能覆盖原文")
    duplicates = store.rows("SELECT id FROM sources WHERE sha256=?", (digest,))
    if duplicates:
        with store.connection() as db:
            db.execute("BEGIN")
            _check_import_context(
                db, source_id, expected_revision, expected_source, expected_details
            )
            return _source_result(db, duplicates[0]["id"])

    if not data.startswith(b"%PDF-"):
        raise ValueError("文件不是可识别的PDF")
    texts = extract_pdf(data, max_pages=MAX_PAGES, max_bytes=MAX_PDF_BYTES)

    if source_id is None:
        title = title.strip() if title is not None else "导入PDF（待核对书目）"
        authors = [] if authors is None else authors
        official_url = "" if official_url is None else official_url.strip()
        if (
            not title
            or not isinstance(authors, list)
            or not all(isinstance(a, str) for a in authors)
        ):
            raise ValueError("请填写有效标题与作者列表")
        if year is not None and (type(year) is not int or not 1 <= year <= 9999):
            raise ValueError("出版年份应为有效整数或留空")
        if official_url:
            try:
                parsed = urlsplit(official_url)
                valid_url = parsed.scheme in {"https", "http"} and bool(parsed.hostname)
            except ValueError:
                valid_url = False
            if not valid_url:
                raise ValueError("正式来源地址须为HTTP或HTTPS页面，或暂时留空待核对")

    access_level = "fulltext" if any(text.strip() for text in texts) else "metadata"
    path = _pdf_path(store, digest)
    created_file = False
    try:
        with store.connection() as db:
            # 锁定写事务后再核对配额和去重，两个上传不能同时越过上限。
            db.execute("BEGIN IMMEDIATE")
            _check_import_context(
                db, source_id, expected_revision, expected_source, expected_details
            )
            target = None
            if source_id is not None:
                target = db.execute(
                    "SELECT * FROM sources WHERE id=?", (source_id,)
                ).fetchone()
                if target is None:
                    raise ValueError("来源不存在")
                if target["sha256"] and target["sha256"] != digest:
                    raise ValueError("该来源已有PDF，不能覆盖原文")
            duplicate = db.execute(
                "SELECT id FROM sources WHERE sha256=?", (digest,)
            ).fetchone()
            if duplicate:
                return _source_result(db, duplicate["id"])
            count = db.execute(
                "SELECT COUNT(*) FROM sources WHERE sha256 IS NOT NULL"
            ).fetchone()[0]
            if count >= MAX_DOCUMENTS:
                raise ValueError("材料库最多保存20份PDF")

            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                # 允许接回内容完全一致的孤立文件，但绝不覆盖不同内容或链接。
                _read_pdf_file(store, digest)
            else:
                created_file = True
                with os.fdopen(descriptor, "wb") as file:
                    file.write(data)
            if target is None:
                source_id = uid()
                db.execute(
                    "INSERT INTO sources(id,title,authors,year,official_url,access_level,sha256,page_count,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        source_id,
                        title,
                        encoded(authors),
                        year,
                        official_url,
                        access_level,
                        digest,
                        len(texts),
                        now(),
                    ),
                )
            else:
                db.execute(
                    "UPDATE sources SET access_level=?,sha256=?,page_count=? WHERE id=?",
                    (access_level, digest, len(texts), source_id),
                )
            db.executemany(
                "INSERT INTO pages(source_id,page_number,text) VALUES(?,?,?)",
                ((source_id, number, text) for number, text in enumerate(texts, 1)),
            )
            result = _source_result(db, source_id)
        return result
    except Exception:
        if created_file:
            path.unlink(missing_ok=True)
        raise


def document_pages(store: Store, source_id: str) -> list[dict]:
    """保留原始文本，空页单独标注，不向原文中注入提示文字。"""
    store.get_source(source_id)
    return [
        {
            **page,
            "text_status": "有可提取文本" if page["text"].strip() else "无可提取文本",
        }
        for page in store.get_pages(source_id)
    ]


def _search_normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def search_pages(store: Store, keyword: str) -> list[dict]:
    """NFKC/casefold普通子串匹配，不把%或_解释为SQL通配符。"""
    if not isinstance(keyword, str) or not keyword.strip():
        raise ValueError("请输入非空关键词")
    needle = _search_normalize(keyword)
    pages = store.rows(
        "SELECT p.source_id,p.page_number,p.text,s.title FROM pages p "
        "JOIN sources s ON s.id=p.source_id ORDER BY s.created_at,s.id,p.page_number"
    )
    return [page for page in pages if needle in _search_normalize(page["text"])]


def selected_pages(store: Store, pairs: list[tuple[str, int]]) -> list[dict]:
    """按用户选择顺序读取完整页；重复页只计一次，不静默截断正文。"""
    if not pairs:
        raise ValueError("请至少选择一页原文")
    selected = []
    seen = set()
    total = 0
    with store.connection() as db:
        for source_id, page_number in pairs:
            if (
                not isinstance(source_id, str)
                or type(page_number) is not int
                or page_number < 1
            ):
                raise ValueError("来源或物理页码无效，页码从1开始")
            pair = (source_id, page_number)
            if pair in seen:
                continue
            seen.add(pair)
            row = db.execute(
                "SELECT p.source_id,p.page_number,p.text FROM pages p "
                "JOIN sources s ON s.id=p.source_id WHERE p.source_id=? AND p.page_number=? AND s.sha256 IS NOT NULL",
                pair,
            ).fetchone()
            if row is None:
                raise ValueError("所选来源或PDF物理页码不存在")
            page = dict(row)
            total += len(page["text"])
            if total > MAX_SELECTED_CHARACTERS:
                raise ValueError("单次原文最多24000字符，请缩小页范围；未截断原文")
            selected.append(page)
    if not any(page["text"].strip() for page in selected):
        raise ValueError("所选页无可提取文本，不能进入模型分析；请检查扫描件或OCR")
    return selected
