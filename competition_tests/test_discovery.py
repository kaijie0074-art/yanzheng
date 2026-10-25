"""合成索引/HTTP响应仅用于契约回归，不冒充真实联网验收。"""

from __future__ import annotations

import socket
from io import BytesIO

import httpx
import pytest
from pydantic import ValidationError
from reportlab.pdfgen import canvas

from research_app import discovery, documents
from research_app.models import ModelReply, now
from research_app.storage import Store, encoded, uid


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "research")
    add_revision(result)
    return result


def add_revision(store):
    latest = store.latest_brief()
    revision = latest["revision"] + 1 if latest else 1
    with store.connection() as db:
        db.execute(
            "INSERT INTO briefs VALUES(?,?,?,?,?)",
            (
                uid(),
                revision,
                encoded({"question": f"研究信息检索方法 {revision}"}),
                encoded(["information retrieval"]),
                now(),
            ),
        )


def work(**changes):
    return {
        "id": "https://openalex.org/W100",
        "doi": "https://doi.org/10.1234/Example",
        "title": "Retrieval evidence",
        "publication_year": 2024,
        "authorships": [{"author": {"display_name": "Ada Lovelace"}}],
        "abstract_inverted_index": {"Real": [0], "retrieval": [1], "evidence.": [2]},
        "primary_location": {
            "landing_page_url": "https://aclanthology.org/2024.example/"
        },
        "best_oa_location": {
            "is_oa": True,
            "pdf_url": "https://aclanthology.org/2024.example.pdf",
            "license": "cc-by",
        },
        **changes,
    }


def search_items(store, items=None, provider="openalex"):
    items = [work()] if items is None else items
    payload = (
        {"results": items}
        if provider == "openalex"
        else {"status": "ok", "message": {"items": items}}
    )
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        return discovery.search(
            store, "retrieval evidence", provider=provider, client=client
        )


def included(store, **changes):
    run = search_items(store, [work(**changes)])
    return discovery.include_result(store, run["id"], 0)


class Backend:
    def __init__(self, value, callback=None):
        self.value, self.callback, self.calls = value, callback, []

    def complete_json(self, *, system, payload, schema):
        self.calls.append(payload)
        if self.callback:
            self.callback()
        if isinstance(self.value, Exception):
            raise self.value
        return ModelReply(
            value=schema.model_validate(self.value),
            requested_model="test/model",
            actual_model="test/model",
            backend="test",
            usage=None,
            called_at=now(),
            text=encoded(self.value),
        )


def plan_backend(callback=None):
    return Backend(
        {
            "queries": ["information retrieval", "retrieval evidence"],
            "scope": "公开学术索引",
            "inclusion_criteria": ["研究信息检索的实证论文"],
        },
        callback,
    )


def pdf_data(text="Real page evidence."):
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer)
    if text:
        pdf.drawString(30, 700, text)
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


@pytest.fixture
def public_dns(monkeypatch):
    calls = []

    def resolve(host, port, **kwargs):
        calls.append((host, port))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(discovery.socket, "getaddrinfo", resolve)
    return calls


def test_search_only_uses_real_response_and_persists_without_implicit_import(store):
    query = "检索 '&per-page=1000; --"
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == "api.openalex.org"
        assert request.url.params["search"] == query
        assert request.url.params["per-page"] == "20"
        assert discovery.get_state(store)["runs"][0]["status"] == "pending"
        return httpx.Response(200, json={"results": [work()]})

    with httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        run = discovery.search(store, query, client=client)
        assert not client.is_closed
    item = run["results"][0]
    assert run["status"] == "success" and run["study_revision"] == 1
    assert item["title"] == "Retrieval evidence"
    assert item["authors"] == ["Ada Lovelace"]
    assert item["abstract"] == "Real retrieval evidence."
    assert item["doi"] == "10.1234/example"
    assert item["access_level"] == "abstract"
    assert item["recommendation"] == "" and item["source_id"] is None
    assert len(requests) == 1 and store.list_sources() == []
    assert discovery.get_state(Store(store.data_dir))["runs"] == [run]


def test_crossref_imports_available_abstract_but_not_text_mining_link_as_oa(store):
    run = search_items(
        store,
        [
            {
                "DOI": "10.1234/example",
                "title": ["Crossref paper"],
                "author": [{"given": "Ada", "family": "Lovelace"}],
                "published": {"date-parts": [[2020]]},
                "URL": "https://doi.org/10.1234/example",
                "abstract": "<jats:p>Observed <jats:italic>retrieval</jats:italic> results.</jats:p>",
                "link": [
                    {
                        "URL": "https://aclanthology.org/a.pdf",
                        "content-type": "application/pdf",
                    }
                ],
            }
        ],
        provider="crossref",
    )
    source = discovery.include_result(store, run["id"], 0)
    assert source["year"] == 2020 and source["authors"] == ["Ada Lovelace"]
    assert source["bibliography_status"] == "unverified"
    details = discovery.source_details(store, source["id"])
    assert details["abstract"] == "Observed retrieval results."
    assert details["provider"] == "crossref" and details["pdf_url"] is None
    result = discovery.fetch_fulltext(store, source["id"])
    assert result["status"] == "abstract"
    assert store.get_pages(source["id"]) == []


@pytest.mark.parametrize("query", ["", "   ", "x" * 501, None])
def test_invalid_query_does_not_record_or_request(store, query):
    with pytest.raises(ValueError):
        discovery.search(store, query)
    assert discovery.get_state(store)["runs"] == []


def test_empty_and_capped_results_are_not_false_negative_claims(store):
    empty = search_items(store, [])
    assert empty["status"] == "success" and "不代表文献不存在" in empty["message"]
    capped = search_items(
        store, [work(id=f"https://openalex.org/W{i}") for i in range(25)]
    )
    assert len(capped["results"]) == 20
    assert [item["index"] for item in capped["results"]] == list(range(20))


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(429, text="private quota details"),
        httpx.Response(503, text="private maintenance details"),
        httpx.Response(200, text="<html>not JSON</html>"),
        httpx.Response(200, json={"results": [None]}),
        httpx.Response(302, headers={"location": "http://127.0.0.1/admin"}),
        httpx.Response(
            200,
            content=b"{}",
            headers={"content-length": str(discovery.MAX_RESPONSE_BYTES + 1)},
        ),
    ],
)
def test_search_failures_preserve_safe_error_and_do_not_follow_redirect(
    store, response
):
    requests = []

    def handler(request):
        requests.append(request)
        return response

    with httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        run = discovery.search(store, "retrieval", client=client)
    assert run["status"] == "error" and run["results"] == []
    assert len(requests) == 1
    assert "private" not in encoded(run)
    assert discovery.get_state(Store(store.data_dir))["runs"] == [run]
    with pytest.raises(ValueError):
        discovery.include_result(store, run["id"], 0)


class ChunkStream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False
        self.reads = 0

    def __iter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    def close(self):
        self.closed = True


def test_streamed_search_body_limit_closes_response_without_import(store):
    stream = ChunkStream([b"x" * 65536] * 70)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=stream)
        )
    ) as client:
        run = discovery.search(store, "retrieval", client=client)
    assert run["status"] == "error" and "上限" in run["message"]
    assert stream.closed and stream.reads < 70
    assert store.list_sources() == []


def test_network_failure_is_not_retried_or_leaked(store):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("secret proxy and user path", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        run = discovery.search(store, "retrieval", client=client)
    assert len(calls) == 1 and run["status"] == "error"
    assert "secret" not in encoded(discovery.get_state(store))


def test_plan_is_audited_without_changing_question_or_confirmation(store):
    before = store.latest_brief()
    backend = plan_backend()
    plan = discovery.generate_plan(store, backend)
    assert plan["queries"] == ["information retrieval", "retrieval evidence"]
    assert plan["study_revision"] == before["revision"]
    assert store.latest_brief() == before
    assert discovery.get_state(Store(store.data_dir))["plans"] == [plan]
    assert store.rows("SELECT task,status FROM model_runs") == [
        {"task": "discovery_plan", "status": "success"}
    ]
    assert len(backend.calls) == 1 and discovery.get_state(store)["runs"] == []


def test_model_plan_version_change_preserves_audit_not_current_plan(store):
    with pytest.raises(discovery.DiscoveryConflict):
        discovery.generate_plan(store, plan_backend(lambda: add_revision(store)))
    assert discovery.get_state(store)["plans"] == []
    audit = store.rows("SELECT status,error_type FROM model_runs")[0]
    assert audit == {"status": "failed", "error_type": "REVISION_CHANGED"}


def test_ranking_uses_indices_and_keeps_real_metadata_and_inclusion(store):
    run = search_items(store)
    source = discovery.include_result(store, run["id"], 0)
    backend = Backend(
        {
            "recommendations": [
                {"index": 0, "reason": "摘要涉及检索，但未读取全文，需核查方法。"}
            ]
        }
    )
    ranked = discovery.rank_results(store, backend, run["id"])
    assert ranked["results"][0]["title"] == run["results"][0]["title"]
    assert ranked["results"][0]["source_id"] == source["id"]
    assert (
        ranked["results"][0]["recommendation"]
        == backend.value["recommendations"][0]["reason"]
    )
    assert store.get_source(source["id"])["bibliography_status"] == "unverified"


@pytest.mark.parametrize(
    "recommendations",
    [
        [{"index": 1, "reason": "假索引"}],
        [{"index": 0, "reason": "重复"}, {"index": 0, "reason": "重复"}],
        [{"index": True, "reason": "布尔索引"}],
        [{"index": 0, "reason": "   "}],
    ],
)
def test_invalid_model_references_preserve_results_and_failed_audit(
    store, recommendations
):
    run = search_items(store)
    with pytest.raises((ValueError, ValidationError)):
        discovery.rank_results(
            store, Backend({"recommendations": recommendations}), run["id"]
        )
    persisted = discovery.get_state(store)["runs"][0]
    assert persisted["results"] == run["results"] and persisted["status"] == "success"
    assert "未成功采用" in persisted["message"]
    assert store.rows("SELECT status FROM model_runs")[0]["status"] == "failed"


def test_ranking_backend_failure_preserves_real_results(store):
    run = search_items(store)
    with pytest.raises(RuntimeError):
        discovery.rank_results(store, Backend(RuntimeError("private error")), run["id"])
    assert discovery.get_state(store)["runs"][0]["results"] == run["results"]
    assert "private" not in encoded(discovery.get_state(store))
    assert store.rows("SELECT status FROM model_runs")[0]["status"] == "failed"


def test_ranking_version_change_does_not_apply_suggestions(store):
    run = search_items(store)
    backend = Backend(
        {"recommendations": [{"index": 0, "reason": "相关"}]},
        lambda: add_revision(store),
    )
    with pytest.raises(discovery.DiscoveryConflict):
        discovery.rank_results(store, backend, run["id"])
    assert discovery.get_state(store)["runs"][0]["results"] == run["results"]
    assert (
        store.rows("SELECT error_type FROM model_runs")[0]["error_type"]
        == "REVISION_CHANGED"
    )


def test_search_version_change_retains_actual_historical_results(store):
    def handler(request):
        add_revision(store)
        return httpx.Response(200, json={"results": [work()]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        run = discovery.search(store, "retrieval", study_revision=1, client=client)
    assert (
        run["status"] == "error"
        and run["study_revision"] == 1
        and len(run["results"]) == 1
    )
    with pytest.raises(discovery.DiscoveryConflict):
        discovery.include_result(store, run["id"], 0)
    with pytest.raises(discovery.DiscoveryConflict):
        discovery.search(store, "retrieval", study_revision=1)
    assert len(discovery.get_state(store)["runs"]) == 1


def test_include_deduplicates_doi_across_providers_without_resetting_human_confirmation(
    store,
):
    run = search_items(store)
    first = discovery.include_result(store, run["id"], 0)
    assert first["bibliography_status"] == "unverified"
    assert discovery.include_result(store, run["id"], 0)["id"] == first["id"]
    store.set_bibliography(first["id"], True)
    crossref = search_items(
        store,
        [
            {
                "DOI": "10.1234/EXAMPLE",
                "title": ["Different indexed title"],
                "URL": "https://doi.org/10.1234/EXAMPLE",
            }
        ],
        provider="crossref",
    )
    duplicate = discovery.include_result(store, crossref["id"], 0)
    assert duplicate["id"] == first["id"] and len(store.list_sources()) == 1
    assert (
        duplicate["title"] == "Retrieval evidence"
        and duplicate["bibliography_status"] == "confirmed"
    )
    details = discovery.source_details(store, first["id"])
    assert details["abstract"] == "Real retrieval evidence."
    assert details["pdf_url"] == "https://aclanthology.org/2024.example.pdf"


@pytest.mark.parametrize("identity", ["remote_id", "url"])
def test_include_deduplicates_without_doi(store, identity):
    source = included(store, doi=None)
    changed = {"doi": None}
    if identity == "remote_id":
        changed["primary_location"] = {
            "landing_page_url": "https://aclanthology.org/new-location/"
        }
    else:
        changed["id"] = ""
    duplicate = included(store, **changed)
    assert duplicate["id"] == source["id"] and len(store.list_sources()) == 1


@pytest.mark.parametrize("index", [-1, True, 20, "0"])
def test_invalid_inclusion_indices_never_create_sources(store, index):
    run = search_items(store)
    with pytest.raises(ValueError):
        discovery.include_result(store, run["id"], index)
    assert store.list_sources() == []


def test_successful_pdf_attaches_to_original_source_and_does_not_repeat_download(
    store, public_dns
):
    source = included(store)
    content = pdf_data()
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "aclanthology.org"
        assert request.extensions["sni_hostname"] == "aclanthology.org"
        assert (
            "cookie" not in request.headers and "authorization" not in request.headers
        )
        return httpx.Response(200, content=content)

    with httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "private", "Cookie": "private"},
    ) as client:
        result = discovery.fetch_fulltext(store, source["id"], client=client)
        second = discovery.fetch_fulltext(store, source["id"], client=client)
    assert result["status"] == second["status"] == "fulltext" and len(requests) == 1
    assert result["source_id"] == source["id"] and len(store.list_sources()) == 1
    assert documents.pdf_bytes(store, source["id"]) == content
    assert "Real page evidence." in store.get_pages(source["id"])[0]["text"]
    assert store.get_source(source["id"])["bibliography_status"] == "unverified"
    assert (
        discovery.source_details(Store(store.data_dir), source["id"])[
            "fulltext_attempt"
        ]["status"]
        == "fulltext"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://aclanthology.org/a.pdf",
        "https://aclanthology.org.evil.test/a.pdf",
        "https://127.0.0.1/a.pdf",
        "https://[::1]/a.pdf",
        "https://aclanthology.org:444/a.pdf",
        "https://name:password@aclanthology.org/a.pdf",
        "https://arxiv.org@evil.test/a.pdf",
    ],
)
def test_unsafe_oa_url_never_requests_network_and_keeps_abstract(store, url):
    source = included(store, best_oa_location={"is_oa": True, "pdf_url": url})
    requests = []
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: requests.append(request))
    ) as client:
        result = discovery.fetch_fulltext(store, source["id"], client=client)
    assert result["status"] in {"error", "abstract"} and not requests
    assert store.get_source(source["id"])["access_level"] == "abstract"
    assert store.get_source(source["id"])["sha256"] is None


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1", "::ffff:127.0.0.1"],
)
def test_dns_private_and_mixed_addresses_are_rejected(store, monkeypatch, address):
    source = included(store)
    monkeypatch.setattr(
        discovery.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)),
        ],
    )
    requests = []
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: requests.append(request))
    ) as client:
        result = discovery.fetch_fulltext(store, source["id"], client=client)
    assert (
        result["status"] == "error" and "非公网" in result["message"] and not requests
    )


def test_redirect_escape_is_blocked_before_second_request(store, public_dns):
    source = included(store)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            302, headers={"location": "http://169.254.169.254/latest/meta-data"}
        )

    with httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        result = discovery.fetch_fulltext(store, source["id"], client=client)
    assert result["status"] == "error" and len(requests) == 1
    assert store.get_source(source["id"])["sha256"] is None


def test_valid_redirect_revalidates_dns_and_preserves_host_tls(store, public_dns):
    source = included(store)
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                302, headers={"location": "https://arxiv.org/pdf/2401.00001"}
            )
        assert (
            request.headers["host"] == request.extensions["sni_hostname"] == "arxiv.org"
        )
        return httpx.Response(200, content=pdf_data())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = discovery.fetch_fulltext(store, source["id"], client=client)
    assert result["status"] == "fulltext" and len(requests) == 2
    assert public_dns == [("aclanthology.org", 443), ("arxiv.org", 443)]


@pytest.mark.parametrize(
    "body,headers",
    [
        (b"<html>Sign in</html>", {}),
        (b"%PDF-corrupt", {}),
        (b"%PDF-", {"content-length": str(documents.MAX_PDF_BYTES + 1)}),
        (b"compressed", {"content-encoding": "br"}),
    ],
)
def test_non_pdf_corrupt_oversize_or_encoded_response_preserves_source(
    store, public_dns, body, headers
):
    source = included(store)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, stream=ChunkStream([body]), headers=headers
            )
        )
    ) as client:
        result = discovery.fetch_fulltext(store, source["id"], client=client)
    assert result["status"] == "error"
    assert store.get_source(source["id"])["sha256"] is None
    assert (
        discovery.source_details(store, source["id"])["abstract"]
        == "Real retrieval evidence."
    )
    assert (
        discovery.source_details(store, source["id"])["fulltext_attempt"]["status"]
        == "error"
    )


def test_streamed_pdf_size_limit_and_scan_preserve_abstract(
    store, public_dns, monkeypatch
):
    source = included(store)
    monkeypatch.setattr(documents, "MAX_PDF_BYTES", 65536)
    stream = ChunkStream([b"%PDF-" + b"x" * 65531, b"x" * 65536, b"not read"])
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=stream)
        )
    ) as client:
        result = discovery.fetch_fulltext(store, source["id"], client=client)
    assert result["status"] == "error" and stream.closed and stream.reads == 2
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=pdf_data(""))
        )
    ) as client:
        result = discovery.fetch_fulltext(store, source["id"], client=client)
    saved = store.get_source(source["id"])
    assert result["status"] == saved["access_level"] == "abstract"
    assert saved["sha256"] and saved["page_count"] == 1
    assert not store.get_pages(source["id"])[0]["text"].strip()


def test_closed_location_is_not_downloaded_despite_pdf_url(store):
    source = included(
        store, best_oa_location={"is_oa": False, "pdf_url": "https://arxiv.org/a.pdf"}
    )
    requests = []
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: requests.append(request))
    ) as client:
        result = discovery.fetch_fulltext(store, source["id"], client=client)
    assert result["status"] == "abstract" and not requests


def test_duplicate_pdf_under_other_source_is_not_misrepresented_or_relabelled(
    store, public_dns
):
    content = pdf_data()
    existing = documents.import_pdf(store, content, title="Existing original")
    source = included(store)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=content)
        )
    ) as client:
        result = discovery.fetch_fulltext(store, source["id"], client=client)
    assert result["status"] == "error" and result["source_id"] == source["id"]
    assert store.get_source(source["id"])["sha256"] is None
    assert store.get_source(existing["id"])["title"] == "Existing original"
    assert len(store.list_sources()) == 2


def test_download_research_version_change_keeps_original_metadata(store, public_dns):
    source = included(store)

    def handler(request):
        add_revision(store)
        return httpx.Response(200, content=pdf_data())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(discovery.DiscoveryConflict):
            discovery.fetch_fulltext(store, source["id"], client=client)
    assert store.get_source(source["id"])["sha256"] is None
    assert (
        discovery.source_details(store, source["id"])["fulltext_attempt"]["status"]
        == "error"
    )


def test_no_research_question_never_calls_model_or_network(tmp_path):
    empty = Store(tmp_path / "empty")
    backend = plan_backend()
    with pytest.raises(ValueError):
        discovery.generate_plan(empty, backend)
    with pytest.raises(ValueError):
        discovery.search(empty, "retrieval")
    assert not backend.calls
    assert discovery.get_state(empty) == {"plans": [], "runs": []}
    assert discovery.source_details(empty, "missing") == {}


def test_missing_ranking_index_does_not_partially_apply_recommendations(store):
    run = search_items(store, [work(), work(id="https://openalex.org/W200")])
    backend = Backend({"recommendations": [{"index": 0, "reason": "仅有第一项"}]})
    with pytest.raises(ValueError):
        discovery.rank_results(store, backend, run["id"])
    assert discovery.get_state(store)["runs"][0]["results"] == run["results"]


def test_each_redirect_rejects_dns_rebinding_before_connection(store, monkeypatch):
    source = included(store)
    dns_calls, requests = [], []

    def resolve(*args, **kwargs):
        dns_calls.append(args)
        address = "93.184.216.34" if len(dns_calls) == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]

    monkeypatch.setattr(discovery.socket, "getaddrinfo", resolve)

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "/new.pdf"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = discovery.fetch_fulltext(store, source["id"], client=client)
    assert result["status"] == "error" and "非公网" in result["message"]
    assert len(requests) == 1 and len(dns_calls) == 2


def test_slow_small_chunks_respect_total_transfer_limit(store, public_dns, monkeypatch):
    source = included(store)
    clock = [0.0]
    monkeypatch.setattr(discovery.time, "monotonic", lambda: clock[0])

    class SlowStream(ChunkStream):
        def __iter__(self):
            for chunk in self.chunks:
                self.reads += 1
                clock[0] += 31
                yield chunk

    stream = SlowStream([b"%PDF-", b"x", b"unread"])
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=stream)
        )
    ) as client:
        result = discovery.fetch_fulltext(store, source["id"], client=client)
    assert result["status"] == "error" and "时限" in result["message"]
    assert stream.reads == 2 and stream.closed
    assert store.get_source(source["id"])["sha256"] is None


def test_remote_identity_survives_enrichment_by_other_provider(store):
    first = included(store, doi=None, best_oa_location=None)
    crossref = search_items(
        store,
        [
            {
                "title": [first["title"]],
                "URL": first["official_url"],
            }
        ],
        provider="crossref",
    )
    assert discovery.include_result(store, crossref["id"], 0)["id"] == first["id"]
    duplicate = included(
        store,
        doi=None,
        best_oa_location=None,
        primary_location={"landing_page_url": "https://aclanthology.org/moved/"},
    )
    assert duplicate["id"] == first["id"] and len(store.list_sources()) == 1


@pytest.mark.parametrize("conflict", ["doi", "remote_id", "both", "remote_id_same_doi"])
@pytest.mark.parametrize("fragment", ["", "#different-paper"])
def test_conflicting_strong_identity_never_inherits_shared_url_confirmation(
    store, conflict, fragment
):
    first = included(store, doi=None if conflict == "remote_id" else "10.1234/first")
    store.set_bibliography(first["id"], True)
    original = store.get_source(first["id"])
    original_details = discovery.source_details(store, first["id"])
    second = included(
        store,
        id="https://openalex.org/W200"
        if conflict != "doi"
        else "https://openalex.org/W100",
        doi=None
        if conflict == "remote_id"
        else (
            "10.1234/first" if conflict == "remote_id_same_doi" else "10.1234/second"
        ),
        title="A different paper",
        primary_location={"landing_page_url": original["official_url"] + fragment},
        abstract_inverted_index={"Another": [0], "paper.": [1]},
        best_oa_location={"is_oa": True, "pdf_url": "https://arxiv.org/pdf/second.pdf"},
    )
    assert second["id"] != first["id"] and len(store.list_sources()) == 2
    assert second["bibliography_status"] == "unverified"
    assert store.get_source(first["id"]) == original
    assert discovery.source_details(store, first["id"]) == original_details
    assert discovery.source_details(store, second["id"])["abstract"] == "Another paper."
    assert (
        discovery.source_details(store, second["id"])["pdf_url"]
        == "https://arxiv.org/pdf/second.pdf"
    )


def test_reinclude_result_does_not_follow_source_id_after_identity_edit(store):
    run = search_items(store)
    first = discovery.include_result(store, run["id"], 0)
    store.update_source_metadata(
        first["id"],
        title="Another paper",
        authors=["Other author"],
        year=2025,
        official_url=first["official_url"],
        doi="10.1234/another",
    )
    second = discovery.include_result(store, run["id"], 0)
    assert second["id"] != first["id"]
    assert second["doi"] == "10.1234/example"
    assert store.get_source(first["id"])["doi"] == "10.1234/another"


@pytest.mark.parametrize("changed_fields", ["title_and_url", "title", "url"])
def test_reinclude_without_doi_never_restores_details_after_identity_edit(
    store, changed_fields
):
    run = search_items(store, [work(doi=None)])
    first = discovery.include_result(store, run["id"], 0)
    original_details = discovery.source_details(store, first["id"])
    store.update_source_metadata(
        first["id"],
        title="Another paper" if "title" in changed_fields else first["title"],
        authors=first["authors"],
        year=first["year"],
        official_url=(
            "https://aclanthology.org/another/"
            if "url" in changed_fields
            else first["official_url"]
        ),
        doi=None,
    )
    edited = store.get_source(first["id"])
    assert discovery.source_details(store, first["id"]) == {}

    second = discovery.include_result(store, run["id"], 0)

    assert second["id"] != first["id"]
    assert second["title"] == first["title"]
    assert second["official_url"] == first["official_url"]
    assert second["bibliography_status"] == "unverified"
    assert store.get_source(first["id"]) == edited
    assert edited["access_level"] == "metadata"
    assert discovery.source_details(store, first["id"]) == {}
    assert store.get_pages(first["id"]) == []
    restored = discovery.source_details(store, second["id"])
    assert restored["abstract"] == original_details["abstract"]
    assert restored["pdf_url"] == original_details["pdf_url"]
    assert restored["source_identity"]["title"] == first["title"]
    assert restored["source_identity"]["official_url"] == first["official_url"]
    assert (
        discovery.get_state(store)["runs"][0]["results"][0]["source_id"] == second["id"]
    )
    assert discovery.include_result(store, run["id"], 0)["id"] == second["id"]
    assert len(store.list_sources()) == 2


@pytest.mark.parametrize("doi", [None, "10.1234/example"])
def test_reinclude_preserves_same_paper_author_and_year_corrections(store, doi):
    run = search_items(store, [work(doi=doi, id="")])
    first = discovery.include_result(store, run["id"], 0)
    store.update_source_metadata(
        first["id"],
        title=first["title"],
        authors=["Corrected author"],
        year=2025,
        official_url=first["official_url"],
        doi=doi,
    )
    store.set_bibliography(first["id"], True)
    corrected = store.get_source(first["id"])

    duplicate = discovery.include_result(store, run["id"], 0)

    assert duplicate == corrected
    assert duplicate["bibliography_status"] == "confirmed"
    assert len(store.list_sources()) == 1
    details = discovery.source_details(store, first["id"])
    assert details["source_identity"]["authors"] == ["Corrected author"]
    assert details["source_identity"]["year"] == 2025
    assert details["abstract"] == "Real retrieval evidence."
    assert details["pdf_url"] == "https://aclanthology.org/2024.example.pdf"


def test_reinclude_same_doi_preserves_corrected_title_and_url(store):
    run = search_items(store)
    first = discovery.include_result(store, run["id"], 0)
    store.update_source_metadata(
        first["id"],
        title="Corrected paper title",
        authors=first["authors"],
        year=first["year"],
        official_url="https://doi.org/10.1234/example",
        doi=first["doi"],
    )
    store.set_bibliography(first["id"], True)
    corrected = store.get_source(first["id"])
    assert discovery.source_details(store, first["id"]) == {}

    duplicate = discovery.include_result(store, run["id"], 0)

    assert duplicate["id"] == first["id"]
    assert duplicate["title"] == corrected["title"]
    assert duplicate["official_url"] == corrected["official_url"]
    assert duplicate["bibliography_status"] == "confirmed"
    assert len(store.list_sources()) == 1
    details = discovery.source_details(store, first["id"])
    assert details["source_identity"]["title"] == corrected["title"]
    assert details["source_identity"]["official_url"] == corrected["official_url"]
    assert details["abstract"] == "Real retrieval evidence."
    assert details["pdf_url"] == "https://aclanthology.org/2024.example.pdf"


@pytest.mark.parametrize("legacy", [False, True])
def test_stale_source_identity_never_downloads_old_pdf(store, legacy):
    source = included(store)
    if legacy:
        detail = discovery.source_details(store, source["id"])
        del detail["source_identity"]
        with store.connection() as db:
            db.execute(
                "UPDATE paper_details SET metadata_json=? WHERE source_id=?",
                (encoded(detail), source["id"]),
            )
    # 模拟旧客户端直接改写来源而保留旧发现详情，不能依赖新编辑入口清理才安全。
    with store.connection() as db:
        db.execute(
            "UPDATE sources SET doi='10.1234/another' WHERE id=?", (source["id"],)
        )
    requests = []
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: requests.append(request))
    ) as client:
        with pytest.raises(discovery.DiscoveryConflict, match="身份不一致"):
            discovery.fetch_fulltext(store, source["id"], client=client)
    assert requests == []
    assert store.get_source(source["id"])["sha256"] is None
    assert store.get_pages(source["id"]) == []


def test_legacy_unchanged_identity_still_downloads(store, public_dns):
    source = included(store)
    detail = discovery.source_details(store, source["id"])
    del detail["source_identity"]
    with store.connection() as db:
        db.execute(
            "UPDATE paper_details SET metadata_json=? WHERE source_id=?",
            (encoded(detail), source["id"]),
        )
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=pdf_data())
        )
    ) as client:
        result = discovery.fetch_fulltext(store, source["id"], client=client)
    assert result["status"] == "fulltext"
    assert store.get_source(source["id"])["sha256"]


@pytest.mark.parametrize("change", ["title", "doi", "confirmation", "details"])
def test_download_rejects_changed_source_or_details_snapshot(store, public_dns, change):
    source = included(store)

    def handler(request):
        with store.connection() as db:
            if change == "title":
                db.execute(
                    "UPDATE sources SET title='Changed title' WHERE id=?",
                    (source["id"],),
                )
            elif change == "doi":
                db.execute(
                    "UPDATE sources SET doi='10.1234/another' WHERE id=?",
                    (source["id"],),
                )
            elif change == "confirmation":
                db.execute(
                    "UPDATE sources SET bibliography_status='confirmed' WHERE id=?",
                    (source["id"],),
                )
            else:
                details = discovery.source_details(store, source["id"])
                details["abstract"] = "Changed abstract"
                db.execute(
                    "UPDATE paper_details SET metadata_json=? WHERE source_id=?",
                    (encoded(details), source["id"]),
                )
        return httpx.Response(200, content=pdf_data())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(discovery.DiscoveryConflict):
            discovery.fetch_fulltext(store, source["id"], client=client)
    assert store.get_source(source["id"])["sha256"] is None
    assert store.get_source(source["id"])["access_level"] != "fulltext"
    assert store.get_pages(source["id"]) == []
    assert (
        discovery.source_details(store, source["id"])["fulltext_attempt"]["status"]
        == "error"
    )


@pytest.mark.parametrize("change", ["revision", "identity", "details"])
def test_import_final_transaction_rejects_change_after_pdf_extraction(
    store, public_dns, monkeypatch, change
):
    source = included(store)
    extract = documents.extract_pdf

    def extract_then_change(data, **kwargs):
        texts = extract(data, **kwargs)
        if change == "revision":
            add_revision(store)
        else:
            with store.connection() as db:
                if change == "identity":
                    db.execute(
                        "UPDATE sources SET doi='10.1234/another' WHERE id=?",
                        (source["id"],),
                    )
                else:
                    details = discovery.source_details(store, source["id"])
                    details["license"] = "changed"
                    db.execute(
                        "UPDATE paper_details SET metadata_json=? WHERE source_id=?",
                        (encoded(details), source["id"]),
                    )
        return texts

    monkeypatch.setattr(documents, "extract_pdf", extract_then_change)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=pdf_data())
        )
    ) as client:
        with pytest.raises(discovery.DiscoveryConflict):
            discovery.fetch_fulltext(store, source["id"], client=client)
    assert store.get_source(source["id"])["sha256"] is None
    assert store.get_source(source["id"])["access_level"] != "fulltext"
    assert store.get_pages(source["id"]) == []
    assert (
        discovery.source_details(store, source["id"])["fulltext_attempt"]["status"]
        == "error"
    )


def test_strong_doi_match_wins_over_earlier_shared_landing_url(store):
    weak_id = store.add_source(
        title="Unidentified landing page",
        authors=[],
        year=None,
        official_url="https://aclanthology.org/2024.example/",
    )
    strong_id = store.add_source(
        title="Verified paper",
        authors=["Ada Lovelace"],
        year=2024,
        official_url="https://doi.org/10.1234/example",
        doi="10.1234/example",
    )
    store.set_bibliography(strong_id, True)
    source = included(store)
    assert source["id"] == strong_id
    assert source["bibliography_status"] == "confirmed"
    assert discovery.source_details(store, weak_id) == {}
    assert len(store.list_sources()) == 2


def test_ambiguous_landing_url_creates_independent_source_and_reinclude_is_stable(
    store,
):
    for title in ("First unidentified paper", "Second unidentified paper"):
        store.add_source(
            title=title,
            authors=[],
            year=None,
            official_url="https://aclanthology.org/2024.example/",
        )
    run = search_items(store, [work(id="", doi=None)])
    first = discovery.include_result(store, run["id"], 0)
    duplicate = discovery.include_result(store, run["id"], 0)
    assert first["id"] == duplicate["id"]
    assert len(store.list_sources()) == 3
    assert first["bibliography_status"] == "unverified"
