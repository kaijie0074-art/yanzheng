"""固定官方材料的受控HTTP契约；不联网、不下载真实论文。"""

import hashlib
import io
import json
import zipfile

import httpx
import pytest
from pypdf import PdfWriter

from research_app import case_materials as materials


def zip_bytes(entries):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return stream.getvalue()


def pdf_bytes():
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


@pytest.fixture
def responses():
    return {
        materials.UCI_PAGE_URL: b'<html>License: <a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a></html>',
        materials.UCI_ZIP_URL: zip_bytes(
            [
                ("SMSSpamCollection", b"ham\thello\nspam\tprize\textra tab\n"),
                ("readme", "官方测试说明\n".encode()),
            ]
        ),
        **{url: pdf_bytes() for url in materials.PAPER_URLS.values()},
    }


def client_for(responses, requests=None):
    def handle(request):
        if requests is not None:
            requests.append(str(request.url))
        body = responses[str(request.url)]
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, content=body)

    return httpx.Client(transport=httpx.MockTransport(handle), trust_env=False)


def test_records_real_hashes_sources_and_permission_boundaries(tmp_path, responses):
    requests = []
    with client_for(responses, requests) as client:
        result = materials.prepare_materials(tmp_path, client=client)
    assert set(requests) == set(responses)
    assert materials.DATA_PAPER_DOI not in requests
    assert result["status"] == "ready"
    assert result["dataset_license"]["status"] == "official_page_statement_checked"
    assert result["dataset_license"]["matched_text"]
    assert result["model_excerpt_permission"] == "requires_user_confirmation"
    assert result["sms_model_transmission_allowed"] is False
    assert set(result["files"]) == {*materials.DOWNLOADS, *materials.MEMBER_LIMITS}
    for name, record in result["files"].items():
        content = (tmp_path / "case" / name).read_bytes()
        assert record["sha256"] == hashlib.sha256(content).hexdigest()
        assert record["size_bytes"] == len(content)
        assert record["url"] in responses
        assert record["final_url"] == record["url"]
        assert record["retrieved_at"]
        assert record["external_export_allowed"] is False
    assert (tmp_path / "case" / "readme").read_text("utf-8") == "官方测试说明\n"
    for name in materials.PAPER_URLS:
        assert result["files"][name]["license"]["name"] is None
        assert result["files"][name]["license"]["status"] == "not_verified"
        assert result["files"][name]["bibliography_status"] == "unverified"
    original = result["metadata_only_sources"][0]
    assert original["access_level"] == "metadata"
    assert original["fulltext_read"] is False
    assert original["fetched"] is False
    assert original["sha256"] is None
    assert json.loads((tmp_path / "case" / "materials.json").read_text()) == result
    assert not (tmp_path / "sms_manifest.json").exists()  # 审计由实验模块负责。


def test_zip_traversal_members_are_ignored_never_extracted(
    tmp_path, responses, monkeypatch
):
    responses[materials.UCI_ZIP_URL] = zip_bytes(
        [
            ("SMSSpamCollection", b"ham\tlocal only\n"),
            ("readme", b"known member"),
            ("../../escaped.txt", b"bad"),
            ("/absolute.txt", b"bad"),
            ("nested/SMSSpamCollection", b"bad"),
        ]
    )

    def forbidden(*args, **kwargs):
        pytest.fail("禁止调用ZIP解压接口")

    monkeypatch.setattr(zipfile.ZipFile, "extract", forbidden)
    monkeypatch.setattr(zipfile.ZipFile, "extractall", forbidden)
    with client_for(responses) as client:
        result = materials.prepare_materials(tmp_path, client=client)
    assert result["ignored_zip_member_count"] == 3
    assert {p.name for p in (tmp_path / "case").iterdir()} == {
        *materials.DOWNLOADS,
        *materials.MEMBER_LIMITS,
        "materials.json",
    }
    assert (
        tmp_path / "case" / "SMSSpamCollection"
    ).read_bytes() == b"ham\tlocal only\n"
    assert not (tmp_path / "escaped.txt").exists()


@pytest.mark.parametrize(
    "entries",
    [
        [("readme", b"missing data")],
        [("SMSSpamCollection", b"ham\thi")],
        [("SMSSpamCollection", b"ham\thi"), ("readme", b"\x00binary")],
        [("SMSSpamCollection", b"ham\thi"), ("readme", b"")],
    ],
)
def test_missing_or_invalid_known_member_fails_without_writes(
    tmp_path, responses, entries
):
    responses[materials.UCI_ZIP_URL] = zip_bytes(entries)
    with client_for(responses) as client, pytest.raises(materials.MaterialError):
        materials.prepare_materials(tmp_path, client=client)
    assert not (tmp_path / "case").exists()


def test_official_latin1_readme_preserves_bytes_and_author_name(tmp_path, responses):
    original = b"Created by Jos\xe9 Mar\xeda G\xf3mez Hidalgo. 5574 messages.\n"
    responses[materials.UCI_ZIP_URL] = zip_bytes(
        [
            ("SMSSpamCollection", b"ham\tlocal\nspam\tprize\n"),
            ("readme", original),
        ]
    )
    with client_for(responses) as client:
        result = materials.prepare_materials(tmp_path, client=client)
    assert (tmp_path / "case" / "readme").read_bytes() == original
    text, encoding = materials.readme_text(original)
    assert "José María Gómez Hidalgo" in text
    assert result["files"]["readme"]["sha256"] == hashlib.sha256(original).hexdigest()
    assert result["files"]["readme"]["interpreted_encoding"] == encoding


@pytest.mark.parametrize(
    "body", [b"<html>rate limited</html>", b"%PDF-1.7\nnot really a PDF"]
)
def test_non_pdf_or_damaged_pdf_not_accepted(tmp_path, responses, body):
    responses[materials.PAPER_URLS["P12-2018.pdf"]] = body
    with (
        client_for(responses) as client,
        pytest.raises(materials.MaterialError, match="PDF"),
    ):
        materials.prepare_materials(tmp_path, client=client)
    assert not (tmp_path / "case").exists()


def test_license_requires_actual_official_page_evidence(tmp_path, responses):
    responses[materials.UCI_PAGE_URL] = (
        b"<html>SMS dataset without a license statement</html>"
    )
    with (
        client_for(responses) as client,
        pytest.raises(materials.MaterialError, match="未检出"),
    ):
        materials.prepare_materials(tmp_path, client=client)
    assert not (tmp_path / "case").exists()


def test_validated_cache_performs_no_http(tmp_path, responses):
    with client_for(responses) as client:
        expected = materials.prepare_materials(tmp_path, client=client)
    with client_for({}) as client:
        actual = materials.prepare_materials(tmp_path, client=client)
    assert actual == expected


@pytest.mark.parametrize(
    "filename",
    ["sms.zip", "SMSSpamCollection", "readme", "dataset.html", "P12-2018.pdf"],
)
def test_cache_tampering_stops_without_overwriting_or_redownloading(
    tmp_path, responses, filename
):
    with client_for(responses) as client:
        materials.prepare_materials(tmp_path, client=client)
    path = tmp_path / "case" / filename
    original = path.read_bytes()
    tampered = bytes([original[0] ^ 1]) + original[1:]
    path.write_bytes(tampered)
    with (
        client_for({}) as client,
        pytest.raises(materials.MaterialError, match="校验失败"),
    ):
        materials.prepare_materials(tmp_path, client=client)
    assert path.read_bytes() == tampered


def test_manifest_cannot_redirect_cache_file_paths(tmp_path, responses):
    with client_for(responses) as client:
        materials.prepare_materials(tmp_path, client=client)
    path = tmp_path / "case" / "materials.json"
    manifest = json.loads(path.read_text())
    manifest["files"]["../../outside"] = manifest["files"].pop("readme")
    path.write_text(json.dumps(manifest))
    with (
        client_for({}) as client,
        pytest.raises(materials.MaterialError, match="校验失败"),
    ):
        materials.prepare_materials(tmp_path, client=client)


def test_untracked_existing_material_not_blindly_trusted(tmp_path):
    (tmp_path / "case").mkdir()
    path = tmp_path / "case" / "SMSSpamCollection"
    path.write_bytes(b"ham\tmanual file\n")
    with (
        client_for({}) as client,
        pytest.raises(materials.MaterialError, match="缺少materials.json"),
    ):
        materials.prepare_materials(tmp_path, client=client)
    assert path.read_bytes() == b"ham\tmanual file\n"


def test_https_official_redirect_is_recorded(tmp_path, responses):
    target = "https://archive.ics.uci.edu/static/official-redirect.zip"
    responses[target] = responses[materials.UCI_ZIP_URL]
    responses[materials.UCI_ZIP_URL] = httpx.Response(302, headers={"location": target})
    with client_for(responses) as client:
        manifest = materials.prepare_materials(tmp_path, client=client)
    assert manifest["files"]["sms.zip"]["url"] == materials.UCI_ZIP_URL
    assert manifest["files"]["sms.zip"]["final_url"] == target


@pytest.mark.parametrize(
    "target",
    [
        "https://example.com/data.zip",
        "http://archive.ics.uci.edu/data.zip",
        "https://user:pass@archive.ics.uci.edu/file",
    ],
)
def test_redirect_to_unapproved_endpoint_rejected_before_request(
    tmp_path, responses, target
):
    requests = []
    responses[materials.UCI_ZIP_URL] = httpx.Response(302, headers={"location": target})
    with (
        client_for(responses, requests) as client,
        pytest.raises(materials.MaterialError, match="未允许"),
    ):
        materials.prepare_materials(tmp_path, client=client)
    assert target not in requests


def test_http_failure_says_official_manual_import_no_third_party(tmp_path, responses):
    responses[materials.UCI_ZIP_URL] = httpx.Response(403)
    with (
        client_for(responses) as client,
        pytest.raises(
            materials.MaterialError, match="同一UCI或ACL官方页面手工下载"
        ) as error,
    ):
        materials.prepare_materials(tmp_path, client=client)
    assert "HTTP 403" in str(error.value)
    assert "不改用第三方" in str(error.value)


@pytest.mark.parametrize("declared_length", [None, "99999999"])
def test_bounded_download_with_or_without_length_header(
    tmp_path, responses, monkeypatch, declared_length
):
    monkeypatch.setitem(materials.DOWNLOADS, "sms.zip", (materials.UCI_ZIP_URL, 16))
    headers = {"content-length": declared_length} if declared_length else {}

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"x" * 10
            yield b"x" * 10

    responses[materials.UCI_ZIP_URL] = httpx.Response(
        200, headers=headers, stream=Stream()
    )
    with (
        client_for(responses) as client,
        pytest.raises(materials.MaterialError, match="大小限制"),
    ):
        materials.prepare_materials(tmp_path, client=client)
    assert not (tmp_path / "case").exists()
