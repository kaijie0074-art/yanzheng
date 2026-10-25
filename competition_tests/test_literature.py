from __future__ import annotations

import httpx
import pytest

from research_app.literature import (
    CROSSREF_WORKS,
    get_search_runs,
    screen_result,
    search_crossref,
    verify_doi,
)
from research_app.models import now
from research_app.storage import Store, encoded, uid


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "data")
    with instance.connection() as db:
        db.execute(
            "INSERT INTO briefs VALUES(?,?,?,?,?)",
            (
                uid(),
                1,
                encoded({"question": "英文短信分类"}),
                encoded(["spam classification"]),
                now(),
            ),
        )
    return instance


def metadata(**changes):
    return {
        "DOI": "10.1234/example",
        "title": [" A baseline study "],
        "author": [{"given": "Ada", "family": "Lovelace"}, {"name": "Research Group"}],
        "published": {"date-parts": [[2020, 4]]},
        "URL": "https://doi.org/10.1234/example",
        **changes,
    }


def client_for(items):
    return httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"status": "ok", "message": {"items": items}}
            )
        )
    )


def test_exact_query_fixed_endpoint_and_refresh_persistence(store):
    query = "spam '&rows=999; DROP TABLE sources; -- 中文"
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        assert str(request.url).startswith(CROSSREF_WORKS + "?")
        assert dict(request.url.params) == {
            "query.bibliographic": query,
            "rows": "20",
            "select": "DOI,title,author,published,URL",
        }
        pending = get_search_runs(store)
        assert len(pending) == 1 and pending[0]["status"] == "pending"
        return httpx.Response(
            200, json={"status": "ok", "message": {"items": [metadata()]}}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        run = search_crossref(store, query, client=client)
        assert not client.is_closed
    assert run["status"] == "success"
    assert run["query"] == query
    assert run["study_revision"] == 1
    assert run["results"] == [
        {
            "title": "A baseline study",
            "authors": ["Ada Lovelace", "Research Group"],
            "year": 2020,
            "doi": "10.1234/example",
            "official_url": "https://doi.org/10.1234/example",
            "access_level": "metadata",
        }
    ]
    assert run["raw"]["message"]["items"] == [metadata()]
    assert run["screening"][0]["include"] is None
    assert get_search_runs(Store(store.data_dir)) == [run]
    assert len(requests) == 1
    assert store.list_sources() == []


def test_empty_results_are_success_not_missing_literature(store):
    with client_for([]) as client:
        run = search_crossref(store, "unlikely query", client=client)
    assert run["status"] == "success"
    assert run["error_type"] is None
    assert run["results"] == run["screening"] == []
    assert "不代表文献不存在" in run["message"]
    assert get_search_runs(Store(store.data_dir)) == [run]


@pytest.mark.parametrize(
    ("status", "payload", "error"),
    [
        (429, {"message": "rate limited"}, "RATE_LIMIT"),
        (503, {"message": "unavailable"}, "HTTP"),
        (404, {"message": "not found"}, "NOT_FOUND"),
        (200, {"status": "ok", "message": {}}, "INVALID_RESPONSE"),
        (200, {"status": "ok", "message": {"items": [None]}}, "INVALID_RESPONSE"),
        (200, {"status": "failed", "message": {"items": []}}, "INVALID_RESPONSE"),
        (200, [], "INVALID_RESPONSE"),
    ],
)
def test_failures_persist_raw_response_without_invented_results(
    store, status, payload, error
):
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json=payload)
        )
    ) as client:
        run = search_crossref(store, "classification", client=client)
    assert run["status"] == "error" and run["error_type"] == error
    assert run["raw"] == payload
    assert run["results"] == []
    assert get_search_runs(Store(store.data_dir)) == [run]
    with pytest.raises(ValueError):
        screen_result(store, run["id"], 0, include=True, reason="不能纳入失败响应")
    assert store.list_sources() == []


def test_non_json_response_is_retained(store):
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text="<html>maintenance</html>")
        )
    ) as client:
        run = search_crossref(store, "text", client=client)
    assert run["error_type"] == "INVALID_RESPONSE"
    assert run["raw"] == "<html>maintenance</html>"
    assert get_search_runs(Store(store.data_dir)) == [run]


@pytest.mark.parametrize("error_class", [httpx.ConnectError, httpx.ReadTimeout])
def test_network_failure_is_recorded_without_retry_or_local_error_details(
    store, error_class
):
    calls = []

    def handler(request):
        calls.append(request)
        raise error_class("private-proxy-credential", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        run = search_crossref(store, "text", client=client)
    assert len(calls) == 1
    assert run["status"] == "error" and run["error_type"] == "NETWORK"
    assert run["raw"] is None
    assert "private-proxy-credential" not in encoded(run)
    assert get_search_runs(Store(store.data_dir)) == [run]


def test_redirects_are_not_followed_even_with_injected_client(store):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})

    with httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        run = search_crossref(store, "query", client=client)
    assert run["error_type"] == "HTTP"
    assert len(requests) == 1
    assert requests[0].url.host == "api.crossref.org"


def test_unconfirmed_brief_and_empty_query_never_request(tmp_path):
    store = Store(tmp_path)

    def unexpected(request):
        pytest.fail("无效输入不应触发网络请求")

    with httpx.Client(transport=httpx.MockTransport(unexpected)) as client:
        for query in ["", " \t\n", "valid query"]:
            with pytest.raises(ValueError):
                search_crossref(store, query, client=client)
        with store.connection() as db:
            db.execute(
                "INSERT INTO briefs VALUES(?,?,?,?,?)", (uid(), 1, "{}", "[]", "")
            )
        with pytest.raises(ValueError, match="确认研究问题"):
            search_crossref(store, "valid query", client=client)
    assert get_search_runs(store) == []


def test_missing_title_author_and_partial_dates_do_not_break_results(store):
    items = [
        {},
        metadata(title=None, author=None, published=None),
        metadata(
            author=[None, {}, {"family": "OnlyFamily"}], published={"date-parts": [[]]}
        ),
        metadata(
            title=[None, "", "Valid title"],
            published={"date-parts": [[True]]},
            URL="javascript:alert(1)",
        ),
    ]
    with client_for(items) as client:
        run = search_crossref(store, "partial metadata", client=client)
    assert run["status"] == "success"
    first, second, third, fourth = run["results"]
    assert first == {
        "title": "",
        "authors": [],
        "year": None,
        "doi": None,
        "official_url": "",
        "access_level": "metadata",
    }
    assert second["title"] == "" and second["authors"] == [] and second["year"] is None
    assert third["authors"] == ["OnlyFamily"] and third["year"] is None
    assert fourth["title"] == "Valid title" and fourth["year"] is None
    assert fourth["official_url"] == "https://doi.org/10.1234/example"
    with pytest.raises(ValueError, match="缺少标题"):
        screen_result(store, run["id"], 0, include=True, reason="信息尚不完整")
    assert screen_result(store, run["id"], 0, include=False, reason="信息不足") is None
    assert store.list_sources() == []


def test_screening_reasons_and_repeated_selection_are_persistent(store):
    with client_for([metadata()]) as client:
        run = search_crossref(store, "baseline", client=client)
    for reason in ["", "  "]:
        with pytest.raises(ValueError, match="理由"):
            screen_result(store, run["id"], 0, include=True, reason=reason)
    assert screen_result(store, run["id"], 0, include=False, reason="暂不相关") is None
    assert store.list_sources() == []
    reason = "直接相关'; DROP TABLE sources; --"
    source_id = screen_result(store, run["id"], 0, include=True, reason=reason)
    assert screen_result(store, run["id"], 0, include=True, reason=reason) == source_id
    source = store.get_source(source_id)
    assert source["access_level"] == "metadata"
    assert source["bibliography_status"] == "unverified"
    assert source["page_count"] == 0 and source["sha256"] is None
    assert source["inclusion_reason"] == reason
    assert screen_result(store, run["id"], 0, include=False, reason="重新审视") is None
    assert store.get_source(source_id)["exclusion_reason"] == "重新审视"
    assert (
        screen_result(store, run["id"], 0, include=True, reason="确认相关") == source_id
    )
    reopened = Store(store.data_dir)
    assert len(reopened.list_sources()) == 1
    item = get_search_runs(reopened)[0]["screening"][0]
    assert item["source_id"] == source_id and item["include"] is True
    assert item["reason"] == "确认相关"
    assert reopened.get_source(source_id)["bibliography_status"] == "unverified"


def test_same_doi_across_queries_reuses_source(store):
    with client_for([metadata()]) as client:
        one = search_crossref(store, "one", client=client)
        two = search_crossref(store, "two", client=client)
    first = screen_result(store, one["id"], 0, include=True, reason="相关")
    second = screen_result(store, two["id"], 0, include=True, reason="再次检索")
    assert first == second
    assert len(store.list_sources()) == 1
    assert len(get_search_runs(Store(store.data_dir))) == 2


@pytest.mark.parametrize("index", [-1, 1, True, "0"])
def test_invalid_index_never_selects_another_item(store, index):
    with client_for([metadata()]) as client:
        run = search_crossref(store, "query", client=client)
    with pytest.raises(ValueError):
        screen_result(store, run["id"], index, include=True, reason="相关")
    assert store.list_sources() == []
    assert get_search_runs(store)[0]["screening"][0]["include"] is None


def test_unknown_run_id_is_parameterized(store):
    with pytest.raises(ValueError):
        screen_result(store, "' OR 1=1 --", 0, include=True, reason="相关")
    assert get_search_runs(store) == [] and store.list_sources() == []


def test_doi_is_one_encoded_path_and_only_returns_unconfirmed_metadata():
    doi = "10.1234/a/b?x=1#fragment%25"
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.url.host == "api.crossref.org"
        assert request.url.raw_path == b"/works/10.1234%2Fa%2Fb%3Fx%3D1%23fragment%2525"
        assert not request.url.query and not request.url.fragment
        return httpx.Response(200, json={"status": "ok", "message": metadata(DOI=doi)})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = verify_doi(doi, client=client)
        assert not client.is_closed
    assert len(calls) == 1
    assert result["status"] == "success"
    assert result["result"]["doi"] == doi
    assert result["result"]["access_level"] == "metadata"
    assert "人工" in result["message"]
    assert "bibliography_status" not in result["result"]


@pytest.mark.parametrize(
    "doi",
    [
        "",
        "10.1234/",
        "10.1234/a b",
        "10.1234/a\x00",
        "https://evil.test/a",
        "file:///etc/passwd",
    ],
)
def test_invalid_doi_never_accesses_user_url(doi):
    def unexpected(request):
        pytest.fail("DOI格式错误不应触发网络请求")

    with httpx.Client(transport=httpx.MockTransport(unexpected)) as client:
        with pytest.raises(ValueError):
            verify_doi(doi, client=client)


@pytest.mark.parametrize(
    ("status", "payload", "error"),
    [
        (404, {"message": "not found"}, "NOT_FOUND"),
        (429, {"message": "rate limited"}, "RATE_LIMIT"),
        (
            200,
            {"status": "ok", "message": metadata(DOI="10.1234/different")},
            "INVALID_RESPONSE",
        ),
        (200, {"status": "ok", "message": None}, "INVALID_RESPONSE"),
    ],
)
def test_doi_failures_never_invent_bibliography(status, payload, error):
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json=payload)
        )
    ) as client:
        result = verify_doi("10.1234/example", client=client)
    assert result["status"] == "error" and result["error_type"] == error
    assert result["result"] is None and result["raw"] == payload
    assert result["message"]
