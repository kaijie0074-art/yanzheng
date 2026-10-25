"""显式下载固定公开案例；不调用模型，不代表用户确认论文片段发送权。

运行：uv run python -m research_app.case_materials
论文全文仅留本地；外部过程包不得附带未经确认可再分发的PDF。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import stat
import zipfile
from contextlib import nullcontext
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from pypdf import PdfReader

UCI_PAGE_URL = "https://archive.ics.uci.edu/dataset/228/sms+spam+collection"
UCI_ZIP_URL = "https://archive.ics.uci.edu/static/public/228/sms+spam+collection.zip"
PAPER_URLS = {
    "P12-2018.pdf": "https://aclanthology.org/P12-2018.pdf",
    "E17-2068.pdf": "https://aclanthology.org/E17-2068.pdf",
}
DATA_PAPER_DOI = "https://doi.org/10.1145/2034691.2034742"
DOWNLOADS = {
    "dataset.html": (UCI_PAGE_URL, 5 * 1024 * 1024),
    "sms.zip": (UCI_ZIP_URL, 5 * 1024 * 1024),
    **{name: (url, 20 * 1024 * 1024) for name, url in PAPER_URLS.items()},
}
MEMBER_LIMITS = {"SMSSpamCollection": 10 * 1024 * 1024, "readme": 1024 * 1024}
SCHEMA_VERSION = 1
MANUAL_GUIDANCE = (
    "可从同一UCI或ACL官方页面手工下载合法材料并导入材料库；"
    "请保留来源和哈希记录。本入口不改用第三方数据，不绕过访问限制。"
)


class MaterialError(ValueError):
    """可向本地界面展示、无网络凭据的材料准备错误。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _allowed_url(url: str, original: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = urlsplit(original).hostname
        allowed = (
            {host} if host != "aclanthology.org" else {host, "www.aclanthology.org"}
        )
        return (
            parsed.scheme == "https"
            and parsed.hostname in allowed
            and parsed.port in (None, 443)
            and not parsed.username
            and not parsed.password
            and not parsed.fragment
        )
    except ValueError:
        return False


def _download(client: httpx.Client, url: str, limit: int) -> tuple[bytes, dict]:
    current = url
    try:
        for _ in range(6):
            if not _allowed_url(current, url):
                raise MaterialError("官方下载重定向到未允许的地址，已停止。")
            with client.stream(
                "GET", current, follow_redirects=False, timeout=45
            ) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        raise MaterialError("官方下载重定向缺少目标地址。")
                    current = urljoin(current, location)
                    continue
                if response.status_code != 200:
                    raise MaterialError(
                        f"官方材料下载失败（HTTP {response.status_code}）。"
                    )
                length = response.headers.get("content-length")
                if length is not None:
                    try:
                        declared = int(length)
                    except ValueError as exc:
                        raise MaterialError("官方下载长度声明无效。") from exc
                    if declared < 0 or declared > limit:
                        raise MaterialError("官方材料超过下载大小限制。")
                body = bytearray()
                # iter_bytes统计解压后的HTTP正文，防止压缩传输绕过限制。
                for chunk in response.iter_bytes(chunk_size=65536):
                    if len(body) + len(chunk) > limit:
                        raise MaterialError("官方材料超过下载大小限制。")
                    body.extend(chunk)
                data = bytes(body)
                if not data:
                    raise MaterialError("官方材料响应为空。")
                return data, {
                    "url": url,
                    "final_url": str(response.url),
                    "retrieved_at": _now(),
                    "sha256": _sha(data),
                    "size_bytes": len(data),
                    "acquisition": "official_http_download",
                }
        raise MaterialError("官方下载重定向次数超过限制。")
    except httpx.HTTPError as exc:
        raise MaterialError("官方下载网络失败或超时。") from exc


def _license_evidence(page: bytes) -> dict:
    try:
        text = unescape(page.decode("utf-8", errors="strict"))
    except UnicodeDecodeError as exc:
        raise MaterialError("UCI数据页面无法按UTF-8读取，不能确认许可声明。") from exc
    # 同时兼容可见文字和页面中的官方许可证链接；不把数据许可套用于论文。
    match = re.search(
        r"creativecommons\.org/licenses/by/4\.0/?|CC\s+BY\s+4\.0|"
        r"Creative\s+Commons\s+Attribution\s+4\.0",
        text,
        flags=re.IGNORECASE,
    )
    if match is None:
        raise MaterialError(
            "本次官方页面未检出CC BY 4.0声明，停止准备；请人工核对官方许可。"
        )
    return {
        "name": "CC BY 4.0",
        "status": "official_page_statement_checked",
        "source_url": UCI_PAGE_URL,
        "page_sha256": _sha(page),
        "matched_text": match.group(0),
        "redistribution_boundary": "数据按官方页声明的CC BY 4.0使用，须保留署名及许可；不延伸到论文全文。",
    }


def readme_text(data: bytes) -> tuple[str, str]:
    """只对官方说明文件作字节保真解码；SMS数据仍须独立严格UTF-8解析。"""
    if any(byte < 32 and byte not in (9, 10, 13) for byte in data):
        raise MaterialError("官方readme包含二进制控制字符，停止准备。")
    try:
        return data.decode("utf-8", errors="strict"), "utf-8"
    except UnicodeDecodeError:
        # 实际官方readme作者重音字符为E9/ED/F3，与Latin-1映射一致。
        # 不更改原始字节，也不声称官方声明了该编码。
        return data.decode("latin-1"), "latin-1 (byte-preserving interpretation)"


def _zip_members(data: bytes) -> tuple[dict[str, bytes], int]:
    extracted = {}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            for name, limit in MEMBER_LIMITS.items():
                matches = [entry for entry in entries if entry.filename == name]
                if len(matches) != 1:
                    raise MaterialError(f"官方ZIP必须恰好包含一个已知成员{name}。")
                entry = matches[0]
                mode = entry.external_attr >> 16
                if (
                    entry.is_dir()
                    or stat.S_ISLNK(mode)
                    or entry.flag_bits & 1
                    or entry.file_size > limit
                ):
                    raise MaterialError(
                        f"ZIP成员{name}类型、加密状态或大小不符合要求。"
                    )
                # 永不extract/extractall：只读取两个精确名称，其他成员不落盘。
                with archive.open(entry) as stream:
                    content = stream.read(limit + 1)
                if not content or len(content) > limit:
                    raise MaterialError(f"ZIP成员{name}为空或超过大小限制。")
                extracted[name] = content
            ignored = len(entries) - len(MEMBER_LIMITS)
    except (
        zipfile.BadZipFile,
        RuntimeError,
        NotImplementedError,
        OSError,
        EOFError,
    ) as exc:
        raise MaterialError("官方ZIP损坏或压缩格式不受支持。") from exc
    readme_text(extracted["readme"])
    return extracted, ignored


def _validate_pdf(data: bytes, filename: str) -> None:
    if not data.startswith(b"%PDF-"):
        raise MaterialError(f"{filename}并非PDF，不能将错误网页保存成论文。")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted or not 1 <= len(reader.pages) <= 100:
            raise MaterialError(f"{filename}已加密、无页或超过100页限制。")
    except MaterialError:
        raise
    except Exception as exc:
        raise MaterialError(f"{filename}无法解析为有效PDF。") from exc


def _paper_metadata(filename: str) -> dict:
    if filename == "P12-2018.pdf":
        title, authors, year = (
            "Baselines and Bigrams: Simple, Good Sentiment and Topic Classification",
            ["Sida Wang", "Christopher D. Manning"],
            2012,
        )
        boundary = "用于讨论简单文本分类基线；不是SMS实验，也不是本项目管道的原样复现。"
    else:
        title, authors, year = (
            "Bag of Tricks for Efficient Text Classification",
            ["Armand Joulin", "Edouard Grave", "Piotr Bojanowski", "Tomas Mikolov"],
            2017,
        )
        boundary = (
            "用于讨论简单高效基线及迁移边界；不借用论文分数或速度作为本项目结果。"
        )
    return {
        "title": title,
        "authors": authors,
        "year": year,
        "access_level": "fulltext",
        "bibliography_status": "unverified",
        "license": {
            "name": None,
            "status": "not_verified",
            "redistribution_boundary": "公开可访问不等于已确认再分发授权；仅留本地，不随外部成果包导出全文。",
        },
        "external_export_allowed": False,
        "model_excerpt_permission": "requires_user_confirmation",
        "interpretation_boundary": boundary,
    }


def _load_cache(case_dir: Path) -> dict | None:
    manifest_path = case_dir / "materials.json"
    names = {*DOWNLOADS, *MEMBER_LIMITS}
    if not manifest_path.exists():
        if any((case_dir / name).exists() for name in names):
            raise MaterialError(
                "已有材料缺少materials.json，不能验证下载记录；请先保留并人工核对，勿盲目覆盖。"
            )
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest["schema_version"] != SCHEMA_VERSION
            or manifest["status"] != "ready"
            or set(manifest["files"]) != names
        ):
            raise ValueError("manifest")
        contents = {}
        for name in names:
            path = case_dir / name
            record = manifest["files"][name]
            limit = DOWNLOADS[name][1] if name in DOWNLOADS else MEMBER_LIMITS[name]
            if not path.is_file() or path.stat().st_size > limit:
                raise ValueError("file")
            data = path.read_bytes()
            if _sha(data) != record["sha256"] or len(data) != record["size_bytes"]:
                raise ValueError("hash")
            if name in DOWNLOADS:
                url = DOWNLOADS[name][0]
                if record["url"] != url or not _allowed_url(record["final_url"], url):
                    raise ValueError("source")
                datetime.fromisoformat(record["retrieved_at"])
            contents[name] = data
        license_record = _license_evidence(contents["dataset.html"])
        if manifest["dataset_license"] != license_record:
            raise ValueError("license")
        members, ignored = _zip_members(contents["sms.zip"])
        if ignored != manifest["ignored_zip_member_count"]:
            raise ValueError("members")
        for name, content in members.items():
            if contents[name] != content:
                raise ValueError("member")
        for name in PAPER_URLS:
            _validate_pdf(contents[name], name)
            for key, value in _paper_metadata(name).items():
                if manifest["files"][name].get(key) != value:
                    raise ValueError("paper")
        return manifest
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise MaterialError(
            "材料缓存或清单校验失败，可能已被修改；已停止且不覆盖文件，请人工核对后重新准备。"
        ) from exc


def prepare_materials(
    data_dir: str | Path = "data", *, client: httpx.Client | None = None
) -> dict:
    """显式准备固定材料。client仅用于受控测试，不开放URL或下载脚本入口。"""
    case_dir = Path(data_dir) / "case"
    if case_dir.is_symlink() or any(
        (case_dir / name).is_symlink()
        for name in (
            *DOWNLOADS,
            *MEMBER_LIMITS,
            "materials.json",
        )
    ):
        raise MaterialError("材料目录或固定文件不能是符号链接。")
    cached = _load_cache(case_dir)
    if cached is not None:
        return cached
    context = (
        nullcontext(client)
        if client is not None
        else httpx.Client(trust_env=False, timeout=45)
    )
    contents, files = {}, {}
    try:
        with context as http:
            for name, (url, limit) in DOWNLOADS.items():
                data, record = _download(http, url, limit)
                contents[name], files[name] = data, record
                if name == "dataset.html":
                    license_record = _license_evidence(data)
                elif name == "sms.zip":
                    members, ignored = _zip_members(data)
                else:
                    _validate_pdf(data, name)
                    record.update(_paper_metadata(name))
    except MaterialError as exc:
        raise MaterialError(f"{exc} {MANUAL_GUIDANCE}") from exc
    for name in ("dataset.html", "sms.zip"):
        files[name]["license"] = license_record
        files[name]["external_export_allowed"] = False
    for name, content in members.items():
        contents[name] = content
        files[name] = {
            "url": UCI_ZIP_URL,
            "final_url": files["sms.zip"]["final_url"],
            "retrieved_at": files["sms.zip"]["retrieved_at"],
            "acquisition": "known_zip_member",
            "archive_member": name,
            "archive_sha256": files["sms.zip"]["sha256"],
            "sha256": _sha(content),
            "size_bytes": len(content),
            "license": license_record,
            "external_export_allowed": False,
            "model_transmission_allowed": False,
        }
    files["readme"]["interpreted_encoding"] = readme_text(members["readme"])[1]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "status": "ready",
        "prepared_at": _now(),
        "dataset_page": UCI_PAGE_URL,
        "official_reported_sample_count": 5574,
        "sample_count_note": "5574为官方元数据参考值，实际解析数由experiment.audit独立记录，不凑数。",
        "dataset_license": license_record,
        "ignored_zip_member_count": ignored,
        "files": files,
        "metadata_only_sources": [
            {
                "official_url": DATA_PAPER_DOI,
                "doi": "10.1145/2034691.2034742",
                "access_level": "metadata",
                "bibliography_status": "unverified",
                "fulltext_read": False,
                "fetched": False,
                "sha256": None,
                "license": {"name": None, "status": "not_verified"},
                "note": "仅保留数据原论文DOI引用；前期访问记录为403，本入口不重新下载，未读全文，不引用其未读实验结论。",
            }
        ],
        "scope_note": "两篇公开论文与官方数据页构成案例材料包，不是系统综述或穷尽检索。",
        "model_excerpt_permission": "requires_user_confirmation",
        "sms_model_transmission_allowed": False,
        "external_bundle_policy": "不附论文全文、原始短信、ZIP或本地原始材料；仅可输出必要来源与核验记录。",
    }
    # 所有下载与格式检查成功后才落盘；清单最后写入，半途失败不能冒充完整缓存。
    case_dir.mkdir(parents=True, exist_ok=True)
    for name, content in contents.items():
        (case_dir / name).write_bytes(content)
    (case_dir / "materials.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="显式准备固定UCI/ACL公开材料，不调用模型。"
    )
    parser.add_argument("--data-dir", default="data", help="本地数据目录（默认data）")
    args = parser.parse_args()
    try:
        manifest = prepare_materials(args.data_dir)
    except (MaterialError, OSError) as exc:
        message = (
            str(exc)
            if isinstance(exc, MaterialError)
            else "本地材料文件无法读写，请检查目录权限。"
        )
        print(json.dumps({"status": "error", "message": message}, ensure_ascii=False))
        return 1
    print(
        json.dumps(
            {
                "status": manifest["status"],
                "file_count": len(manifest["files"]),
                "dataset_license": manifest["dataset_license"],
                "notice": "论文全文仅存本地；发送片段前仍须由使用者确认权利。短信原文不发送给模型。",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
