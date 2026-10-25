"""合成HTTP响应覆盖安全及消费者行为；不冒充真实搜索或外网验收。"""

from __future__ import annotations

import base64
import hashlib
import socket
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
from html import escape

import httpcore
import httpx
import pytest

from research_app import web_access


@pytest.fixture
def public_dns(monkeypatch):
    calls = []

    def resolve(host, port, **kwargs):
        calls.append((host, port))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(web_access.socket, "getaddrinfo", resolve)
    return calls


def page(body=None, *, title="公开研究方法", headers=None):
    body = (
        body
        if body is not None
        else "<main><h1>研究方法</h1><p>这是一段足够长的实际网页正文，用于解释文献检索与研究设计，以及证据核验的边界。</p></main>"
    )
    return httpx.Response(
        200,
        content=f"<!doctype html><html><head><title>{title}</title></head><body>{body}</body></html>".encode(),
        headers={"content-type": "text/html; charset=utf-8", **(headers or {})},
    )


def search_page(query="研究方法", *, entries=None, empty=False):
    if entries is None:
        entries = [
            (f"https://papers.example/{i}", f"论文{i}", "实际搜索摘要，不是已读原文。")
            for i in range(8)
        ]
    items = "".join(
        f'<li class="b_algo"><h2><a href="{escape(url, quote=True)}">{escape(title)}</a></h2><div class="b_caption"><p>{escape(snippet)}</p></div></li>'
        for url, title, snippet in entries
    )
    if empty:
        items = '<li class="b_no">没有与此查询相符的搜索结果，请调整关键词重新进行检索。</li>'
    return page(
        f'<input name="q" value="{escape(query, quote=True)}"><a href="https://advert.example/">导航广告不应纳入</a><ol id="b_results">{items}</ol>',
        title=f"{query} - 搜索",
    )


def test_search_returns_real_items_only_and_keeps_query_and_time(public_dns):
    calls = []
    query = "研究 & 检索?x=1"

    def handler(request):
        calls.append(request)
        assert request.url.params["q"] == query
        return search_page(query)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = web_access.search_web(query, client=client)
    assert len(calls) == 1
    assert result["provider"] == "bing"
    assert result["query"] == query
    assert datetime.fromisoformat(result["searched_at"]).tzinfo is not None
    assert len(result["results"]) == 5
    assert result["results"][0] == {
        "url": "https://papers.example/0",
        "title": "论文0",
        "snippet": "实际搜索摘要，不是已读原文。",
    }
    assert all("advert" not in item["url"] for item in result["results"])


def test_search_decodes_bing_links_deduplicates_and_rejects_unsafe_urls(public_dns):
    target = "https://papers.example/research"
    tracking = "https://www.bing.com/ck/a?u=a1" + base64.urlsafe_b64encode(
        target.encode()
    ).decode().rstrip("=")
    entries = [
        (tracking, "真实标题", "实际摘要"),
        (target + "#section", "重复条目", "重复摘要"),
        ("javascript:alert(1)", "脚本", "不能使用"),
        ("https://user:secret@papers.example/", "含账户", "不能使用"),
        ("http://papers.example/", "HTTP", "不能使用"),
        ("https://papers.example/other", "另一条", "另一段真实摘要"),
    ]
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: search_page(entries=entries))
    ) as client:
        result = web_access.search_web("研究方法", client=client)
    assert [item["url"] for item in result["results"]] == [
        target,
        "https://papers.example/other",
    ]


def test_explicit_empty_search_is_not_a_fake_success_page(public_dns):
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: search_page(empty=True))
    ) as client:
        assert web_access.search_web("研究方法", client=client)["results"] == []


@pytest.mark.parametrize(
    "response,code",
    [
        (page(), "SEARCH_FORMAT"),
        (search_page("别的查询"), "SEARCH_FORMAT"),
        (
            page(
                '<div id="b_results"><p>这是没有真实条目也没有空结果标识的一段网页正文。</p></div>'
            ),
            "SEARCH_FORMAT",
        ),
        (
            page(
                '<form id="challenge-form">请验证访问</form>', title="Just a moment..."
            ),
            "CHALLENGE",
        ),
        (
            httpx.Response(
                200,
                text='<?xml version="1.0"?><rss/>',
                headers={"content-type": "text/xml"},
            ),
            "NOT_WEBPAGE",
        ),
    ],
)
def test_search_rejects_challenge_non_search_and_wrong_query(
    public_dns, response, code
):
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: response)
    ) as client:
        with pytest.raises(web_access.WebAccessError) as caught:
            web_access.search_web("研究方法", client=client)
    assert caught.value.code == code


@pytest.mark.parametrize("query", [None, "", "  ", "a" * 501, "hello\nworld"])
def test_invalid_queries_never_contact_dns(public_dns, query):
    with pytest.raises(web_access.WebAccessError) as caught:
        web_access.search_web(query)
    assert caught.value.code == "INVALID_QUERY"
    assert not public_dns


def test_read_extracts_visible_text_and_hash_without_scripts_forms_or_hidden_nodes(
    public_dns,
):
    response = page("""<nav>不相关导航</nav><script>steal_credentials()</script><style>body { color: red }</style>
        <main><h1>可核验研究</h1><p>引用：<b>这是一段原始证据</b>，保留实际文字用于核验。</p>
        <p>第二段讨论适用条件，不声称自动通过人工复核。</p><div hidden>隐藏诱导</div>
        <span style="display: none">隐藏提示</span><form>提交私人信息</form></main><footer>页脚广告</footer>""")
    expected_hash = hashlib.sha256(response.content).hexdigest()
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: response)
    ) as client:
        result = web_access.read_page(
            "https://papers.example/research#evidence", client=client
        )
    assert result["url"] == result["final_url"] == "https://papers.example/research"
    assert result["title"] == "公开研究方法"
    assert "引用：这是一段原始证据，保留实际文字用于核验。" in result["text"]
    for forbidden in (
        "steal",
        "不相关导航",
        "隐藏诱导",
        "隐藏提示",
        "提交私人信息",
        "页脚广告",
        "color: red",
    ):
        assert forbidden not in result["text"]
    assert result["content_sha256"] == expected_hash
    assert result["extracted_characters"] == len(result["text"])
    assert not result["truncated"]
    assert datetime.fromisoformat(result["retrieved_at"]).tzinfo is not None


def test_long_plain_text_reports_full_extracted_size_and_explicit_truncation(
    public_dns,
):
    content = "真实的纯文本研究资料。" * 7000
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=content))
    ) as client:
        result = web_access.read_page("https://papers.example/data.txt", client=client)
    assert result["text"] == content[:60000]
    assert result["truncated"] is True
    assert result["extracted_characters"] == len(content)
    assert result["title"] == ""


def test_redirects_are_pinned_and_cannot_inherit_client_credentials(public_dns):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == request.extensions["sni_hostname"]
        assert request.headers["connection"] == "close"
        assert request.headers["accept-encoding"] == "identity"
        for name in ("authorization", "proxy-authorization", "cookie", "x-api-key"):
            assert name not in request.headers
        assert "private_param" not in request.url.params
        if len(requests) == 1:
            return httpx.Response(
                302,
                headers={
                    "location": "https://other.example/article",
                    "set-cookie": "secret=private; Path=/",
                },
            )
        return page()

    with httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
        auth=("private", "secret"),
        cookies={"session": "secret"},
        headers={
            "Authorization": "private",
            "Cookie": "private",
            "Proxy-Authorization": "private",
            "X-Api-Key": "private",
        },
        params={"private_param": "secret"},
    ) as client:
        result = web_access.read_page("https://papers.example/start", client=client)
        assert not client.is_closed
    assert result["final_url"] == "https://other.example/article"
    assert [r.extensions["sni_hostname"] for r in requests] == [
        "papers.example",
        "other.example",
    ]
    assert public_dns == [("papers.example", 443), ("other.example", 443)]


@pytest.mark.parametrize(
    "url",
    [
        "http://papers.example/",
        "https://papers.example:444/",
        "https://user:secret@papers.example/",
        "https://papers.example/\nheader",
        "https://papers.example\\@private/",
        "https://[fe80::1%25en0]/",
        "https://papers.example./",
        "https://papers.example/login",
        "https://papers.example/%6cogin",
        "https://accounts.example/authorize",
    ],
)
def test_unsafe_and_login_urls_fail_before_dns_or_http(public_dns, url):
    with pytest.raises(web_access.WebAccessError) as caught:
        web_access.read_page(url)
    assert caught.value.code == "UNSAFE_URL"
    assert not public_dns


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "100.64.0.1",
        "224.0.0.1",
        "0.0.0.0",
        "192.0.2.1",
        "::1",
        "fd00::1",
        "fe80::1",
        "ff0e::1",
        "::ffff:8.8.8.8",
        "::ffff:127.0.0.1",
        "64:ff9b::7f00:1",
        "64:ff9b:1::1",
        "2002:7f00:1::1",
        "2001::1",
        "2001:20::1",
        "3fff::1",
    ],
)
def test_special_ip_addresses_cannot_be_contacted(monkeypatch, ip):
    monkeypatch.setattr(
        web_access.socket,
        "getaddrinfo",
        lambda *a, **kw: [
            (
                socket.AF_INET6 if ":" in ip else socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (ip, 443),
            )
        ],
    )
    requests = []
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: requests.append(request))
    ) as client:
        with pytest.raises(web_access.WebAccessError) as caught:
            web_access.read_page("https://papers.example/article", client=client)
    assert caught.value.code == "UNSAFE_ADDRESS"
    assert requests == []


def test_mixed_public_private_dns_fails_closed(monkeypatch):
    monkeypatch.setattr(
        web_access.socket,
        "getaddrinfo",
        lambda *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))
            for ip in ("93.184.216.34", "10.0.0.1")
        ],
    )
    with pytest.raises(web_access.WebAccessError) as caught:
        web_access.read_page("https://papers.example/")
    assert caught.value.code == "UNSAFE_ADDRESS"


def test_public_ipv6_is_pinned_without_replacing_tls_hostname(monkeypatch):
    monkeypatch.setattr(
        web_access.socket,
        "getaddrinfo",
        lambda *a, **kw: [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700:4700::1111", 443))
        ],
    )

    def handler(request):
        assert request.url.host == "2606:4700:4700::1111"
        assert (
            request.headers["host"]
            == request.extensions["sni_hostname"]
            == "papers.example"
        )
        return page()

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert web_access.read_page("https://papers.example/", client=client)["text"]


@pytest.mark.parametrize(
    "location,code",
    [
        ("http://127.0.0.1/", "UNSAFE_URL"),
        ("https://login.example/", "UNSAFE_URL"),
        ("https://papers.example:8443/", "UNSAFE_URL"),
        ("https://10.0.0.1/", "UNSAFE_ADDRESS"),
    ],
)
def test_redirect_is_revalidated_before_second_request(monkeypatch, location, code):
    def resolve(host, port, **kwargs):
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("10.0.0.1" if host == "10.0.0.1" else "93.184.216.34", port),
            )
        ]

    monkeypatch.setattr(web_access.socket, "getaddrinfo", resolve)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": location})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(web_access.WebAccessError) as caught:
            web_access.read_page("https://papers.example/", client=client)
    assert caught.value.code == code
    assert len(requests) == 1


def test_search_follows_cn_bing_but_never_switches_provider(public_dns):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                302, headers={"location": "https://cn.bing.com/search?q=research"}
            )
        return search_page("research")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert web_access.search_web("research", client=client)["results"]
    assert [request.headers["host"] for request in requests] == [
        "www.bing.com",
        "cn.bing.com",
    ]
    requests.clear()
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                302, headers={"location": "https://different.example/search"}
            )
        )
    ) as client:
        with pytest.raises(web_access.WebAccessError) as caught:
            web_access.search_web("research", client=client)
    assert caught.value.code == "REDIRECT"


@pytest.mark.parametrize(
    "response,code",
    [
        (httpx.Response(429, text="private server details"), "RATE_LIMIT"),
        (httpx.Response(401, text="private authentication details"), "ACCESS_DENIED"),
        (httpx.Response(403, text="private details"), "ACCESS_DENIED"),
        (httpx.Response(503, text="private server details"), "HTTP"),
        (httpx.Response(302), "REDIRECT"),
        (
            httpx.Response(
                200, content=b"%PDF-1.7", headers={"content-type": "application/pdf"}
            ),
            "NOT_WEBPAGE",
        ),
        (
            httpx.Response(
                200, content=b"%PDF-1.7", headers={"content-type": "text/html"}
            ),
            "NOT_WEBPAGE",
        ),
        (
            httpx.Response(
                200, content=b"binary\x00data", headers={"content-type": "text/plain"}
            ),
            "NOT_WEBPAGE",
        ),
        (page("<script>document.write('dynamic only')</script>"), "EMPTY_PAGE"),
        (
            page("<p>Please verify you are human before accessing this page.</p>"),
            "CHALLENGE",
        ),
        (
            page(
                '<input type="password"><p>输入密码登录后阅读这份资料，必须登录账户以后才能获取这篇正文。</p>'
            ),
            "LOGIN_REQUIRED",
        ),
        (
            httpx.Response(
                200,
                content=b"\xff\xfeinvalid",
                headers={"content-type": "text/plain; charset=utf-8"},
            ),
            "ENCODING",
        ),
        (
            page(headers={"content-length": str(web_access.MAX_RESPONSE_BYTES + 1)}),
            "TOO_LARGE",
        ),
        (page(headers={"content-length": "invalid"}), "INVALID_RESPONSE"),
    ],
)
def test_failures_are_explicit_and_do_not_retry_or_expose_response(
    public_dns, response, code
):
    requests = []

    def handler(request):
        requests.append(request)
        return response

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(web_access.WebAccessError) as caught:
            web_access.read_page("https://papers.example/", client=client)
    assert caught.value.code == code
    assert "private" not in str(caught.value)
    assert len(requests) == 1


class Chunks(httpx.SyncByteStream):
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


def test_compressed_response_is_rejected_before_any_decompression(public_dns):
    stream = Chunks([b"not even a valid gzip stream"])
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/html", "content-encoding": "gzip"},
                stream=stream,
            )
        )
    ) as client:
        with pytest.raises(web_access.WebAccessError) as caught:
            web_access.read_page("https://papers.example/", client=client)
    assert caught.value.code == "ENCODING"
    assert stream.reads == 0 and stream.closed


def test_stream_limit_closes_response_without_returning_partial_page(
    public_dns, monkeypatch
):
    monkeypatch.setattr(web_access, "MAX_RESPONSE_BYTES", 100)
    stream = Chunks([b"a" * 60, b"b" * 60, b"c" * 60])
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "text/plain"}, stream=stream
            )
        )
    ) as client:
        with pytest.raises(web_access.WebAccessError) as caught:
            web_access.read_page("https://papers.example/", client=client)
    assert caught.value.code == "TOO_LARGE"
    assert stream.reads == 2 and stream.closed


def test_dns_timeout_never_starts_http(monkeypatch):
    monkeypatch.setattr(web_access, "REQUEST_TIMEOUT", 0.01)

    def resolve(*args, **kwargs):
        time.sleep(0.08)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(web_access.socket, "getaddrinfo", resolve)
    requests = []
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: requests.append(request))
    ) as client:
        with pytest.raises(web_access.WebAccessError) as caught:
            web_access.read_page("https://papers.example/", client=client)
    assert caught.value.code == "TIMEOUT"
    assert not requests


def test_network_failure_is_safe_and_not_retried(public_dns):
    requests = []

    def handler(request):
        requests.append(request)
        raise httpx.ConnectError("private proxy credentials and hostname")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(web_access.WebAccessError) as caught:
            web_access.read_page("https://papers.example/", client=client)
    assert caught.value.code == "NETWORK"
    assert "private" not in str(caught.value)
    assert len(requests) == 1


def test_total_deadline_applies_across_redirects(public_dns, monkeypatch):
    monkeypatch.setattr(web_access, "TRANSFER_SECONDS", 0.01)
    requests = []

    def handler(request):
        requests.append(request)
        time.sleep(0.02)
        return httpx.Response(302, headers={"location": "/next"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(web_access.WebAccessError) as caught:
            web_access.read_page("https://papers.example/", client=client)
    assert caught.value.code == "TIMEOUT"
    assert len(requests) == 1


def test_default_transport_never_reads_environment_proxy_or_reuses_hop_clients(
    public_dns, monkeypatch
):
    original_client = httpx.Client
    options = []
    created = []
    requests = []
    monkeypatch.setenv(
        "HTTPS_PROXY", "http://private-user:private-secret@127.0.0.1:8888"
    )
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:8888")

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                302, headers={"location": "https://other.example/read"}
            )
        return page()

    def factory(**kwargs):
        options.append(kwargs)
        kwargs.pop("transport").close()
        result = original_client(transport=httpx.MockTransport(handler), **kwargs)
        created.append(result)
        return result

    monkeypatch.setattr(web_access.httpx, "Client", factory)
    result = web_access.read_page("https://papers.example/")
    assert result["final_url"] == "https://other.example/read"
    assert len(created) == 2 and all(client.is_closed for client in created)
    assert all(
        option["trust_env"] is False and option["follow_redirects"] is False
        for option in options
    )


@pytest.mark.parametrize(
    "response",
    [
        page(
            "<p>Sign in to read the complete research article. This is only a login wall.</p>"
        ),
        httpx.Response(
            200,
            text="Sign in to continue reading this research article. No public text is available.",
        ),
    ],
)
def test_login_wall_text_is_not_presented_as_read_evidence(public_dns, response):
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: response)
    ) as client:
        with pytest.raises(web_access.WebAccessError) as caught:
            web_access.read_page("https://papers.example/article", client=client)
    assert caught.value.code == "LOGIN_REQUIRED"


def test_slow_stream_is_closed_on_total_deadline(public_dns, monkeypatch):
    monkeypatch.setattr(web_access, "TRANSFER_SECONDS", 0.02)

    class SlowStream(Chunks):
        def __iter__(self):
            time.sleep(0.03)
            yield b"Real research text must not be returned after the deadline."

    stream = SlowStream([])
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "text/plain"}, stream=stream
            )
        )
    ) as client:
        with pytest.raises(web_access.WebAccessError) as caught:
            web_access.read_page("https://papers.example/article", client=client)
    assert caught.value.code == "TIMEOUT"
    assert stream.closed


def test_dns_timeout_keeps_slot_until_lookup_really_finishes(monkeypatch):
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(web_access, "_DNS_SLOTS", slots)
    monkeypatch.setattr(web_access, "REQUEST_TIMEOUT", 0.01)
    started = threading.Event()
    finish_lookup = threading.Event()
    lookups = []
    requests = []

    def resolve(host, port, **kwargs):
        lookups.append(host)
        if len(lookups) == 1:
            started.set()
            finish_lookup.wait(timeout=2)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    def handler(request):
        requests.append(request)
        return page()

    monkeypatch.setattr(web_access.socket, "getaddrinfo", resolve)
    try:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(web_access.WebAccessError) as timed_out:
                web_access.read_page("https://papers.example/first", client=client)
            assert timed_out.value.code == "TIMEOUT"
            assert started.is_set()
            for _ in range(3):
                with pytest.raises(web_access.WebAccessError) as busy:
                    web_access.read_page("https://papers.example/next", client=client)
                assert busy.value.code == "DNS_BUSY"
            assert lookups == ["papers.example"]
            assert requests == []
            finish_lookup.set()
            assert slots.acquire(timeout=2)
            slots.release()
            monkeypatch.setattr(web_access, "REQUEST_TIMEOUT", 1)
            assert web_access.read_page(
                "https://papers.example/recovered", client=client
            )["text"]
            assert len(lookups) == 2 and len(requests) == 1
    finally:
        finish_lookup.set()


@pytest.mark.parametrize(
    "parser_class", [web_access._PageText, web_access._BingResults]
)
@pytest.mark.parametrize("attack", ["depth", "nodes"])
def test_each_html_parser_rejects_actual_excess_work_before_unbounded_stack_scan(
    parser_class, attack
):
    if attack == "depth":
        html = "<div>" * (web_access.MAX_HTML_DEPTH + 1)
    else:
        html = "<br>" * (web_access.MAX_HTML_NODES + 1)
    parser = parser_class()
    with pytest.raises(web_access.WebAccessError) as caught:
        parser.feed(html)
    assert caught.value.code == "HTML_LIMIT"
    assert len(parser.stack) <= web_access.MAX_HTML_DEPTH
    assert parser.node_count <= web_access.MAX_HTML_NODES + 1


@pytest.mark.parametrize("operation", ["read", "search"])
@pytest.mark.parametrize("attack", ["depth", "nodes"])
def test_excess_html_never_returns_preceding_readable_text_or_search_items(
    public_dns, operation, attack
):
    initial = search_page() if operation == "search" else page()
    suffix = (
        "<div>" * (web_access.MAX_HTML_DEPTH + 1)
        if attack == "depth"
        else "<br>" * (web_access.MAX_HTML_NODES + 1)
    )
    response = httpx.Response(
        200,
        content=initial.content + suffix.encode(),
        headers={"content-type": "text/html; charset=utf-8"},
    )
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: response)
    ) as client:
        with pytest.raises(web_access.WebAccessError) as caught:
            if operation == "search":
                web_access.search_web("研究方法", client=client)
            else:
                web_access.read_page("https://papers.example/article", client=client)
    assert caught.value.code == "HTML_LIMIT"


@pytest.mark.parametrize("mode", ["early_hints", "no_final", "slow_headers"])
def test_real_transport_closes_socket_at_absolute_header_deadline(monkeypatch, mode):
    """真实HTTPX/httpcore/socket路径；仅测试目标替换为本地明文服务器。"""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(2)
    port = listener.getsockname()[1]
    peer_closed = threading.Event()
    stop = threading.Event()
    interim_sent = []

    def serve():
        try:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(0.02)
                request = b""
                until = time.monotonic() + 2
                while b"\r\n\r\n" not in request and time.monotonic() < until:
                    try:
                        chunk = connection.recv(4096)
                    except socket.timeout:
                        continue
                    if not chunk:
                        peer_closed.set()
                        return
                    request += chunk
                if mode == "slow_headers":
                    connection.sendall(b"HTTP/1.1 200 OK\r\n")
                while not stop.is_set() and time.monotonic() < until:
                    if mode == "early_hints":
                        connection.sendall(b"HTTP/1.1 103 Early Hints\r\n\r\n")
                        interim_sent.append(103)
                    elif mode == "slow_headers":
                        connection.sendall(b"X-Padding: still-waiting\r\n")
                    try:
                        if connection.recv(1) == b"":
                            peer_closed.set()
                            return
                    except socket.timeout:
                        continue
        except (BrokenPipeError, ConnectionResetError):
            peer_closed.set()
        except OSError:
            if not stop.is_set():
                raise

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    monkeypatch.setattr(web_access, "TRANSFER_SECONDS", 0.15)
    monkeypatch.setattr(web_access, "REQUEST_TIMEOUT", 1.0)
    monkeypatch.setattr(
        web_access,
        "_public_target",
        lambda url, deadline: (
            f"http://127.0.0.1:{port}/article",
            "papers.example",
            "https://papers.example/article",
        ),
    )
    started = time.monotonic()
    try:
        with pytest.raises(web_access.WebAccessError) as caught:
            web_access.read_page("https://papers.example/article")
        elapsed = time.monotonic() - started
        assert caught.value.code == "TIMEOUT"
        assert elapsed < 0.8
        assert peer_closed.wait(timeout=0.5)
        if mode == "early_hints":
            assert len(interim_sent) >= 2
    finally:
        stop.set()
        listener.close()
        server.join(timeout=2)
    assert not server.is_alive()


def test_real_tls_handshake_deadline_closes_underlying_socket():
    client_socket, peer_socket = socket.socketpair()
    peer_closed = threading.Event()

    def silent_tls_peer():
        with peer_socket:
            peer_socket.settimeout(2)
            while peer_socket.recv(4096):
                pass
            peer_closed.set()

    peer = threading.Thread(target=silent_tls_peer, daemon=True)
    peer.start()
    context = ssl.create_default_context()
    stream = web_access._DeadlineStream(client_socket, time.monotonic() + 0.15)
    started = time.monotonic()
    try:
        with pytest.raises(httpcore.ConnectTimeout):
            stream.start_tls(context, server_hostname="papers.example", timeout=1)
        assert time.monotonic() - started < 0.8
        assert stream.sock.fileno() == -1
        assert peer_closed.wait(timeout=0.5)
    finally:
        stream.close()
        peer.join(timeout=2)
    assert not peer.is_alive()


@pytest.mark.parametrize("mime", ["text/html", "text/plain"])
@pytest.mark.parametrize("padding", [100, 1999, 2001, 3999, 4001, 10000])
@pytest.mark.parametrize(
    "notice,code",
    [
        ("Verify you are human before proceeding.", "CHALLENGE"),
        ("Sign in to read this article.", "LOGIN_REQUIRED"),
    ],
)
def test_known_access_wall_cannot_be_hidden_by_page_length(
    public_dns, mime, padding, notice, code
):
    text = notice + " " + "A" * padding
    response = (
        page(f"<main><p>{text}</p></main>", title="研究资料")
        if mime == "text/html"
        else httpx.Response(200, text=text)
    )
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: response)
    ) as client:
        with pytest.raises(web_access.WebAccessError) as caught:
            web_access.read_page("https://papers.example/article", client=client)
    assert caught.value.code == code


def test_default_https_rejects_ca_trusted_only_through_environment(
    tmp_path, monkeypatch
):
    """临时私有CA仅由环境提供时，真实默认传输仍拒绝且关闭TLS连接。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = datetime.now(timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic Web Access Test CA")]
    )
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    server_key = ec.generate_private_key(ec.SECP256R1())
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.DNSName("papers.example")]
            ),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_path = tmp_path / "synthetic-ca.pem"
    cert_path = tmp_path / "synthetic-server.pem"
    key_path = tmp_path / "synthetic-server-key.pem"
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_path))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path))
    tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls_context.load_cert_chain(cert_path, key_path)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)
    listener.settimeout(3)
    port = listener.getsockname()[1]
    peer_closed = threading.Event()
    stop = threading.Event()
    handshake_errors = []
    server_errors = []
    requests = []
    body = b"<html><body>Public research text from the controlled TLS server.</body></html>"

    def serve():
        try:
            for _ in range(2):
                raw, _ = listener.accept()
                raw.settimeout(2)
                # dup观察实际EOF/RST；TLS告警本身或服务端close都不算客户端已关闭。
                with raw, raw.dup() as monitor:
                    monitor.settimeout(2)
                    with tls_context.wrap_socket(
                        raw, server_side=True, do_handshake_on_connect=False
                    ) as connection:
                        try:
                            connection.do_handshake()
                        except ssl.SSLError as error:
                            handshake_errors.append(str(error))
                            try:
                                while monitor.recv(4096):
                                    pass
                            except ConnectionResetError:
                                pass  # 证书拒绝后TCP复位同样证明对端连接已关闭。
                            peer_closed.set()
                            continue
                        request = b""
                        while b"\r\n\r\n" not in request:
                            chunk = connection.recv(4096)
                            if not chunk:
                                return
                            request += chunk
                        requests.append(request)
                        connection.sendall(
                            b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                            + body
                        )
        except OSError as error:
            if not stop.is_set():
                server_errors.append(str(error))

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    monkeypatch.setattr(
        web_access,
        "_public_target",
        lambda url, deadline: (
            f"https://127.0.0.1:{port}/article",
            "papers.example",
            "https://papers.example/article",
        ),
    )
    try:
        # 正向对照证明证书、名称及TLS服务有效；显式信任CA与从环境继承是不同边界。
        explicit_trust = ssl.create_default_context(cafile=str(ca_path))
        with httpx.Client(verify=explicit_trust, trust_env=False, timeout=2) as control:
            response = control.get(f"https://localhost:{port}/article")
            assert response.status_code == 200 and response.content == body
        with pytest.raises(web_access.WebAccessError) as caught:
            web_access.read_page("https://papers.example/article")
        assert caught.value.code == "NETWORK"
        assert peer_closed.wait(timeout=2), {
            "tls_errors": handshake_errors,
            "server_errors": server_errors,
            "http_requests": len(requests),
        }
        assert len(requests) == 1
        assert len(handshake_errors) == 1
        assert "UNKNOWN_CA" in handshake_errors[0]
        assert not server_errors
    finally:
        stop.set()
        listener.close()
        server.join(timeout=3)
    assert not server.is_alive()
